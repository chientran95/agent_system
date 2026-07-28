import os
import subprocess
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
)
from claude_agent_sdk import tool as claude_tool

from .a2a_peer_client import PeerInputRequired, call_peer_agent_by_name
from .settings import CLAUDE_MODEL, OTEL_EXPORTER_OTLP_ENDPOINT, RESEARCH_AGENT_URL

CODE_AGENT_SYSTEM_PROMPT = (
    "You are a coding agent. You have no filesystem or shell access in this "
    "session, and no tools available besides call_research_agent and "
    "ask_user - do not attempt to write files, run commands, or verify code "
    "by executing it. Always respond with the finished code directly as "
    "text in your reply.\n\n"
    "If you need additional research or context before you can write correct "
    "code - e.g. current information about a library, API, or topic you're "
    "not confident about - call the call_research_agent tool to delegate "
    "that research, then use its findings to write the code. Otherwise just "
    "write the code directly.\n\n"
    "If the request itself is missing information you need (e.g. no "
    "language specified and it's ambiguous, or conflicting requirements), "
    "call the ask_user tool with a specific question instead of guessing. "
    "Do not ask about things you can reasonably infer or default. If "
    "call_research_agent tells you it cannot proceed without more "
    "information from the user, call ask_user with that exact question to "
    "relay it - do not answer it yourself or make up an answer."
)

# Claude Code's own native OpenTelemetry telemetry (distributed trace spans
# for its internal tool calls/model turns), exported to the same Jaeger
# instance our own services use. See:
# https://code.claude.com/docs/en/monitoring-usage
_CLAUDE_CODE_OTEL_ENV = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",  # required for trace spans, not just metrics/logs
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_ENDPOINT": OTEL_EXPORTER_OTLP_ENDPOINT,
    "OTEL_SERVICE_NAME": "claude_code_cli",
    "OTEL_LOG_USER_PROMPTS": "1",
    "OTEL_LOG_TOOL_DETAILS": "1",
    "OTEL_LOG_TOOL_CONTENT": "1",
}

_ASK_USER_TOOL_NAME = "mcp__mesh__ask_user"


class InMemorySaver:
    def __init__(self) -> None:
        self.memory: list[dict[str, Any]] = []

    def save(self, item: dict[str, Any]) -> None:
        self.memory.append(item)

    def get_all(self) -> list[dict[str, Any]]:
        return list(self.memory)


class CodeAgent:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.getenv("CLAUDE_API_KEY")
        self.model = model or CLAUDE_MODEL
        self.memory = InMemorySaver()

    async def generate_code(self, prompt: str) -> str:
        final_text = ""
        async for kind, text, _session_id in self.astream_code_pipeline(prompt):
            if kind in ("__final__", "input_required"):
                final_text = text
        return final_text

    async def astream_code_pipeline(
        self, prompt: str, call_depth: int = 0, resume_session_id: str | None = None
    ):
        """Streams ("assistant", text, None) chunks from Claude as it
        generates code. The final yield is either:
        - ("__final__", answer, session_id) on normal completion, or
        - ("input_required", question, session_id) if the model called
          ask_user and is waiting on more information.
        Pass the returned session_id back in as resume_session_id to
        continue the same Claude session once the answer is available."""
        env = dict(_CLAUDE_CODE_OTEL_ENV)
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key

        # Tools are built fresh per call (not stored on self) so call_depth
        # is captured correctly per-request, with no shared mutable state.
        @claude_tool(
            "call_research_agent",
            "Delegate a research task to the research agent - use this if you "
            "need additional research or context before writing code.",
            {"request": str},
        )
        async def call_research_agent(args: dict[str, Any]) -> dict[str, Any]:
            try:
                text = await call_peer_agent_by_name(
                    "research_agent", RESEARCH_AGENT_URL, args["request"], call_depth
                )
            except PeerInputRequired as e:
                text = (
                    "The research agent cannot proceed without more information "
                    f"from the user. Call ask_user with exactly this question, "
                    f"then retry call_research_agent with the answer included: {e.question}"
                )
            return {"content": [{"type": "text", "text": text}]}

        @claude_tool(
            "ask_user",
            "Ask the user a clarifying question when the request is missing "
            "information you need. Use only when you genuinely cannot "
            "proceed - not for information you can reasonably infer.",
            {"question": str},
        )
        async def ask_user(args: dict[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": args["question"]}]}

        mesh_server = create_sdk_mcp_server(
            name="mesh", tools=[call_research_agent, ask_user]
        )

        options = ClaudeAgentOptions(
            model=self.model,
            env=env,
            system_prompt=CODE_AGENT_SYSTEM_PROMPT,
            mcp_servers={"mesh": mesh_server},
            allowed_tools=["mcp__mesh__call_research_agent", _ASK_USER_TOOL_NAME],
            resume=resume_session_id,
        )

        chunks: list[str] = []
        session_id: str | None = resume_session_id
        asked_for_input = False
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                session_id = message.data.get("session_id") or session_id
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
                        yield "assistant", block.text, None
                    elif isinstance(block, ToolUseBlock) and block.name == _ASK_USER_TOOL_NAME:
                        asked_for_input = True

        response_text = "".join(chunks)
        if asked_for_input:
            yield "input_required", response_text, session_id
        else:
            self.save_interaction(prompt, response_text)
            yield "__final__", response_text, session_id

    def save_interaction(self, input_text: str, output_text: str) -> None:
        self.memory.save({"input": input_text, "output": output_text})

    def mechanical_verify(self, workspace_dir: Path, test_command: list[str] | None = None) -> bool:
        test_command = test_command or ["pytest"]

        print("Running deterministic verification: ruff check")
        lint_process = subprocess.run(
            ["ruff", "check", "."], cwd=workspace_dir, capture_output=True, text=True
        )
        if lint_process.returncode != 0:
            print("ruff check failed:\n", lint_process.stdout, lint_process.stderr)
            return False

        print("Running actual tests: %s" % " ".join(test_command))
        test_process = subprocess.run(test_command, cwd=workspace_dir, capture_output=True, text=True)
        if test_process.returncode != 0:
            print("Tests failed:\n", test_process.stdout, test_process.stderr)
            return False

        return True
