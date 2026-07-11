import os
import subprocess
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from .settings import CLAUDE_MODEL, OTEL_EXPORTER_OTLP_ENDPOINT

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
        async for kind, text in self.astream_code_pipeline(prompt):
            if kind == "__final__":
                final_text = text
        return final_text

    async def astream_code_pipeline(self, prompt: str):
        """Streams ("assistant", text) chunks from Claude as it generates
        code, in the same incremental pieces the SDK itself produces. The
        final tuple has kind == "__final__" and text == the full response."""
        env = dict(_CLAUDE_CODE_OTEL_ENV)
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key

        options = ClaudeAgentOptions(model=self.model, env=env)

        chunks: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
                        yield "assistant", block.text

        response_text = "".join(chunks)
        self.save_interaction(prompt, response_text)
        yield "__final__", response_text

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
