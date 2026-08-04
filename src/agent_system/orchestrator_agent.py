from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from .a2a_peer_client import PeerInputRequired, call_peer_agent_by_name
from .langfuse_tracing import get_langchain_callbacks
from .settings import CODE_AGENT_URL, ORCHESTRATOR_MODEL, RESEARCH_AGENT_URL, WEATHER_AGENT_URL

ROUTING_SYSTEM_PROMPT = (
    "You are the routing orchestrator for a multi-agent system. Decide which "
    "single sub-agent should handle the user's request:\n"
    "- coding_agent: writing, editing, reviewing, or explaining code.\n"
    "- research_agent: researching a topic, or writing/verifying content for "
    "a blog post or article.\n"
    "- weather_agent: weather, forecasts, air quality, or climate questions.\n"
    "Always pick exactly one, even if the request could plausibly fit more "
    "than one - pick whichever is the primary intent."
)

# Safety cap on the "peer paused, fold the answer back into the request text,
# retry" loop in dispatch_node - same pattern and same reasoning as
# research_agent.py's call_code_agent: call_peer_agent_by_name always starts
# a brand-new task at the peer (no true cross-task resume), so a pause is
# handled by re-asking with the answer appended, not by resuming the peer's
# own task. Not an expected number of real rounds.
_MAX_DISPATCH_RETRIES = 3

_AGENT_URLS = {
    "coding_agent": CODE_AGENT_URL,
    "research_agent": RESEARCH_AGENT_URL,
    "weather_agent": WEATHER_AGENT_URL,
}


class RouteDecision(BaseModel):
    """Structured output for the routing decision - required, not optional,
    unlike the clarifying_question fields elsewhere in this project. Same
    reliability argument as ClarifyTopicMiddleware/ToolSelection: a required
    enum field is a more reliable signal than asking the model to freely
    choose which tool to call (ADK's transfer_to_agent tool-choice hits the
    same LiteLLM-Ollama empty-functionCall flakiness documented as a known
    issue in TEST_SCENARIOS.md)."""

    agent_name: Literal["coding_agent", "research_agent", "weather_agent"] = Field(
        description=(
            "coding_agent for writing/editing/reviewing/explaining code; "
            "research_agent for researching a topic or writing/verifying "
            "blog/article content; weather_agent for weather, forecast, air "
            "quality, or climate questions."
        )
    )


class OrchestratorState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    agent_name: str


def _content_to_text(content: Any) -> str:
    """Normalizes a LangChain message's `.content` into plain text - some
    tool/peer results come back as a list of content blocks rather than a
    plain string. Same fix as weather_agent.py's and research_agent.py's."""
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


class OrchestratorAgent:
    def __init__(self) -> None:
        self.model = ChatOllama(model=ORCHESTRATOR_MODEL)
        self.router = self.model.with_structured_output(RouteDecision)
        self.checkpointer = InMemorySaver()
        self._graph = self._build_graph()
        print(f"OrchestratorAgent (langgraph) initialized with LLM=ollama:{ORCHESTRATOR_MODEL}")

    def _build_graph(self):
        async def route_node(state: OrchestratorState) -> dict[str, Any]:
            try:
                decision = await self.router.ainvoke(
                    [SystemMessage(content=ROUTING_SYSTEM_PROMPT), *state["messages"]]
                )
                agent_name = decision.agent_name
            except Exception:
                # Same fallback spirit as weather_agent's select_tools_node -
                # don't leave the graph with no route at all.
                agent_name = "research_agent"
            return {
                "agent_name": agent_name,
                "messages": [AIMessage(content=f"Routing to {agent_name}.")],
            }

        async def dispatch_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
            call_depth = (config.get("configurable") or {}).get("call_depth", 0)
            agent_name = state["agent_name"]
            url = _AGENT_URLS[agent_name]
            current_request = _content_to_text(state["messages"][0].content)
            for _ in range(_MAX_DISPATCH_RETRIES):
                try:
                    answer = await call_peer_agent_by_name(agent_name, url, current_request, call_depth)
                    return {"messages": [AIMessage(content=answer)]}
                except PeerInputRequired as e:
                    clarification = interrupt(e.question)
                    current_request = f"{current_request}\n\nAdditional info: {clarification}"
            raise RuntimeError(f"{agent_name} needed too many rounds of clarification.")

        graph = StateGraph(OrchestratorState)
        graph.add_node("route", route_node)
        graph.add_node("dispatch", dispatch_node)
        graph.set_entry_point("route")
        graph.add_edge("route", "dispatch")
        return graph.compile(checkpointer=self.checkpointer)

    async def astream_route(self, request: str, thread_id: str, call_depth: int = 0):
        """Starts a new run, streaming (node_name, text) progress chunks -
        "route" for the routing decision, "dispatch" for the peer agent's
        response. The final yield is either ("__final__", answer) on
        completion, or ("input_required", question) if the chosen peer
        paused. Pass the same thread_id to aresume_route to continue."""
        async for item in self._astream_graph(
            {"messages": [HumanMessage(content=request)]}, thread_id, call_depth
        ):
            yield item

    async def aresume_route(self, answer: str, thread_id: str, call_depth: int = 0):
        """Resumes a run previously paused (input_required)."""
        async for item in self._astream_graph(Command(resume=answer), thread_id, call_depth):
            yield item

    async def _astream_graph(self, input_value, thread_id: str, call_depth: int):
        config = {
            "configurable": {"thread_id": thread_id, "call_depth": call_depth},
            "callbacks": get_langchain_callbacks(),
        }
        final_text = ""
        interrupted_question: str | None = None
        async for chunk in self._graph.astream(input_value, stream_mode="updates", config=config):
            for node_name, node_output in (chunk or {}).items():
                if node_name == "__interrupt__":
                    interrupted_question = node_output[0].value if node_output else "More information needed."
                    continue
                messages = node_output.get("messages", []) if isinstance(node_output, dict) else []
                for message in messages:
                    text = _content_to_text(getattr(message, "content", None))
                    if not text:
                        continue
                    if getattr(message, "type", None) == "ai":
                        final_text = text
                    yield node_name, text
        if interrupted_question is not None:
            yield "input_required", interrupted_question
        else:
            yield "__final__", final_text
