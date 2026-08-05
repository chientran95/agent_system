import uuid
from typing import Any

from ddgs import DDGS
from deepagents import create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from .a2a_peer_client import PeerInputRequired, call_peer_agent_by_name
from .content_agent import ContentAgent
from .langfuse_tracing import get_langchain_callbacks
from .settings import CLAUDE_API_KEY, CODE_AGENT_URL, RESEARCH_AGENT_MODEL

RESEARCH_SYSTEM_PROMPT = (
    "You are a research agent with these capabilities: the web_search tool, "
    "a content_writer subagent, a call_code_agent tool, and an ask_user "
    "tool. Follow this procedure:\n"
    "1. Call the web_search tool yourself, directly, one or more times, to "
    "gather facts and sources about the topic. Do not delegate this step to "
    "any subagent.\n"
    "2. If the request requires writing or generating code based on your "
    "research findings, call the call_code_agent tool with a clear "
    "description of what code is needed and the relevant findings.\n"
    "3. Once you have enough material (and code, if needed), call the "
    "content_writer subagent exactly once, passing it your research "
    "findings, sources, and any generated code as the task description.\n"
    "4. Return the content_writer subagent's blog post as your final "
    "answer, unchanged - do not summarize, rewrite, or add commentary.\n"
    "Never use the general-purpose subagent for any part of this workflow.\n\n"
    "Only call ask_user when the topic is genuinely too vague or ambiguous "
    "to research meaningfully (e.g. no real subject given, or a term that's "
    "ambiguous enough to materially change what you'd research). For "
    "anything else - scope, angle, length, level of detail - just make a "
    "sensible choice yourself and proceed; never ask about things you can "
    "reasonably infer or default."
)

_MAX_CLARIFICATION_ROUNDS = 3


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via DuckDuckGo and return matching results (title, url, snippet)."""
    results = DDGS().text(query, max_results=max_results)
    if not results:
        return "No results found."
    return "\n".join(
        f"- {r.get('title')}\n  {r.get('href')}\n  {r.get('body')}" for r in results
    )


@tool
def ask_user(question: str) -> str:
    """Ask the user a clarifying question when the research topic is too
    vague or ambiguous to research meaningfully. Use only when you
    genuinely cannot proceed - not for information you can reasonably
    infer or default."""
    return interrupt(question)


@tool
async def call_code_agent(request: str, config: RunnableConfig) -> str:
    """Delegate a code-writing task to the coding agent - use this when the
    user's request needs code generated based on your research findings.
    Pass a clear, self-contained description of what to write, including any
    relevant research context the coding agent will need."""
    call_depth = (config.get("configurable") or {}).get("call_depth", 0)
    current_request = request
    for _ in range(_MAX_CLARIFICATION_ROUNDS):
        try:
            return await call_peer_agent_by_name("coding_agent", CODE_AGENT_URL, current_request, call_depth)
        except PeerInputRequired as e:
            # Bubble the pause up through our own graph (same mechanism as
            # weather_agent's ask_user), then retry with the answer folded in.
            answer = interrupt(e.question)
            current_request = f"{current_request}\n\nAdditional info: {answer}"
    raise RuntimeError("code_agent needed too many rounds of clarification.")


def _content_to_text(content: Any) -> str:
    """Normalizes a LangChain message's `.content` into plain text. Some
    tool results come back as a list of content blocks (e.g. `[{"type":
    "text", "text": "..."}]`) rather than a plain string - yielding that
    list as-is (or its Python repr) breaks any caller expecting a string.
    Same fix as weather_agent.py's, applied here defensively."""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


RESEARCH_CLARIFY_SYSTEM_PROMPT = (
    "You are checking whether a research request has enough information to "
    "act on before any research begins. If the topic is genuinely too "
    "vague or ambiguous to research meaningfully (e.g. no real subject "
    "given, or a term ambiguous enough to materially change what you'd "
    "research), set clarifying_question - do not guess. For everything "
    "else - scope, angle, length, level of detail - leave "
    "clarifying_question null and let the research proceed with sensible "
    "defaults."
)


class ClarificationCheck(BaseModel):
    """Structured output for the pre-research clarification check."""

    clarifying_question: str | None = Field(
        default=None,
        description=(
            "A question to ask the user, ONLY if the research topic is "
            "genuinely too vague or ambiguous to research meaningfully. "
            "Leave this null for everything else - never ask about scope, "
            "angle, length, or other things you can reasonably infer or "
            "default."
        ),
    )


class ClarifyTopicMiddleware(AgentMiddleware):
    """Runs once, before any research/model work begins, checking whether
    the request is answerable at all. A required-or-null structured-output
    field is a more reliable signal than asking the model to separately
    decide whether to invoke an ask_user tool - "decide whether to invoke a
    side-channel tool" is exactly the behavior that's been unreliable
    throughout this project (e.g. call_code_agent getting skipped even when
    relevant). Same design as weather_agent's select_tools_node, adapted to
    deepagents' middleware hooks since, unlike weather_agent, this agent's
    graph is built internally by create_deep_agent() rather than by hand.
    See docs/BUG_WEATHER_TOOL_SELECTION.md for the original design writeup."""

    def __init__(self, model: Any) -> None:
        super().__init__()
        self._checker = model.with_structured_output(ClarificationCheck)

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        messages = state["messages"]
        asked_messages: list[Any] = []
        for _ in range(_MAX_CLARIFICATION_ROUNDS):
            try:
                result = await self._checker.ainvoke(
                    [SystemMessage(content=RESEARCH_CLARIFY_SYSTEM_PROMPT), *messages]
                )
            except Exception:
                result = None
            if result and result.clarifying_question:
                answer = interrupt(result.clarifying_question)
                question_msg = AIMessage(content=result.clarifying_question)
                answer_msg = HumanMessage(content=answer)
                messages = [*messages, question_msg, answer_msg]
                asked_messages.extend([question_msg, answer_msg])
                continue
            break
        if asked_messages:
            return {"messages": asked_messages}
        return None


class ResearchAgent:
    def __init__(self) -> None:
        self.content_agent = ContentAgent()
        self.checkpointer = InMemorySaver()
        model = ChatAnthropic(model=RESEARCH_AGENT_MODEL, api_key=CLAUDE_API_KEY)
        self.client = create_deep_agent(
            model=model,
            tools=[web_search, call_code_agent, ask_user],
            subagents=[self.content_agent.as_subagent()],
            system_prompt=RESEARCH_SYSTEM_PROMPT,
            middleware=[ClarifyTopicMiddleware(model)],
            checkpointer=self.checkpointer,
        )
        print(f"ResearchAgent initialized with LLM=anthropic:{RESEARCH_AGENT_MODEL}")

    def research_and_write(self, topic: str, thread_id: str | None = None, call_depth: int = 0) -> str:
        print(f"ResearchAgent: researching topic:\n{topic}\n")
        config = {
            "configurable": {"thread_id": thread_id or str(uuid.uuid4()), "call_depth": call_depth},
            "callbacks": get_langchain_callbacks(),
        }
        result = self.client.invoke({"messages": [{"role": "user", "content": topic}]}, config=config)
        return result["messages"][-1].content

    async def astream_research(self, topic: str, thread_id: str, call_depth: int = 0):
        """Streams (node_name, text) progress chunks as the agent researches
        and delegates to the content subagent / code agent. The final tuple
        is ("__final__", answer) on completion, or ("input_required",
        question) if a call_code_agent delegation needed more information
        from the user. Pass the same thread_id to aresume_research to
        continue after answering."""
        async for item in self._astream_graph(
            {"messages": [{"role": "user", "content": topic}]}, thread_id, call_depth
        ):
            yield item

    async def aresume_research(self, answer: str, thread_id: str, call_depth: int = 0):
        """Resumes a run previously paused (input_required), using the same
        thread_id the original astream_research call used."""
        async for item in self._astream_graph(Command(resume=answer), thread_id, call_depth):
            yield item

    async def _astream_graph(self, input_value, thread_id: str, call_depth: int):
        config = {
            "configurable": {"thread_id": thread_id, "call_depth": call_depth},
            "callbacks": get_langchain_callbacks(),
        }
        final_text = ""
        interrupted_question: str | None = None
        async for chunk in self.client.astream(input_value, stream_mode="updates", config=config):
            for node_name, node_output in (chunk or {}).items():
                if node_name == "__interrupt__":
                    interrupted_question = node_output[0].value if node_output else "More information needed."
                    continue
                messages = node_output.get("messages", []) if isinstance(node_output, dict) else []
                for message in messages:
                    text = _content_to_text(getattr(message, "content", None))
                    tool_calls = getattr(message, "tool_calls", None) or []
                    if not text and tool_calls:
                        # A tool-calling turn normally has empty content (the
                        # decision lives in tool_calls instead) - surface it
                        # as a progress chunk too, so it isn't invisible.
                        text = "; ".join(
                            f"{tc.get('name')}({tc.get('args')})" for tc in tool_calls
                        )
                    if not text:
                        continue
                    if getattr(message, "type", None) == "ai":
                        final_text = text
                    yield node_name, text
        if interrupted_question is not None:
            yield "input_required", interrupted_question
        else:
            yield "__final__", final_text
