from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt

from .langfuse_tracing import get_langchain_callbacks
from .settings import WEATHER_AGENT_MODEL

WEATHER_SYSTEM_PROMPT = (
    "You are a weather assistant with access to many specialized forecast "
    "tools (Open-Meteo, plus national weather-agency models like DWD/GFS/"
    "ECMWF/JMA/MetNo/GEM). Pick the single most appropriate tool for the "
    "request - for example, prefer a country's own national model when the "
    "location clearly falls in that country.\n\n"
    "The forecast tools need numeric latitude/longitude, not a place name. "
    "Whenever the user gives a place name rather than coordinates, always "
    "call geocoding first to resolve it, then use the returned coordinates "
    "in the forecast tool call - never guess coordinates yourself and never "
    "pass a place name string directly to a forecast tool's location "
    "parameters.\n\n"
    "Only call ask_user when the request truly cannot be answered without "
    "more information from the user - specifically:\n"
    "- No location was given at all.\n"
    "- The place name is genuinely ambiguous (e.g. multiple well-known "
    "cities share the name, and it materially changes the answer).\n"
    "For everything else - units, output format, which weather variables "
    "to include, timezone, forecast length, which model to use - just pick "
    "a sensible default yourself and proceed. Never ask about defaults; "
    "only ask about missing or ambiguous facts you cannot infer. When "
    "calling weather_forecast, omit the optional `models` parameter unless "
    "the user explicitly names a specific weather model - it defaults "
    "sensibly on its own.\n\n"
    "If a tool call returns an error, that is not missing information from "
    "the user - retry the same request with different or default parameters "
    "first. Only fall back to ask_user if you are still stuck after that, "
    "and make sure the question you ask accurately reflects what actually "
    "went wrong, not a generic guess."
)

_MCP_SERVER_CONFIG = {
    "open_meteo": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "-p", "open-meteo-mcp-server", "open-meteo-mcp-server"],
    }
}


class WeatherState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@tool
def ask_user(question: str) -> str:
    """Ask the user a clarifying question when their request is missing
    information needed to call a weather tool correctly (e.g. no location,
    an ambiguous place name). Use only when you genuinely cannot proceed -
    not for information you can reasonably infer or default."""
    return interrupt(question)


class WeatherAgent:
    def __init__(self) -> None:
        self.mcp_client = MultiServerMCPClient(_MCP_SERVER_CONFIG)
        # The Open-Meteo MCP server's 17 tool schemas alone run to ~40K
        # tokens; Ollama silently truncates to its default 4096-token
        # context if not told otherwise, which drops the tool definitions
        # entirely and makes the model think it has no tools available.
        self.model = ChatOllama(model=WEATHER_AGENT_MODEL, num_ctx=65536)
        self.checkpointer = InMemorySaver()
        self._graph = None
        print(f"WeatherAgent initialized with LLM=ollama:{WEATHER_AGENT_MODEL}")

    async def _ensure_graph(self):
        """Builds the graph lazily, since loading MCP tools is async and we
        don't want to block __init__."""
        if self._graph is not None:
            return self._graph

        mcp_tools = await self.mcp_client.get_tools()
        tools = [*mcp_tools, ask_user]
        model_with_tools = self.model.bind_tools(tools)

        async def agent_node(state: WeatherState) -> dict[str, Any]:
            response = await model_with_tools.ainvoke(
                [SystemMessage(content=WEATHER_SYSTEM_PROMPT), *state["messages"]]
            )
            return {"messages": [response]}

        graph = StateGraph(WeatherState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", ToolNode(tools))
        graph.set_entry_point("agent")
        graph.add_conditional_edges("agent", tools_condition)
        graph.add_edge("tools", "agent")
        self._graph = graph.compile(checkpointer=self.checkpointer)
        return self._graph

    async def astream_query(self, query: str, thread_id: str):
        """Starts a new run, streaming (node_name, text) progress chunks as
        the agent works. thread_id doubles as the A2A task ID, so a paused
        run can be resumed by referencing the same task. The final yield is
        either ("__final__", answer) on completion, or ("input_required",
        question) if the ask_user tool paused the run - pass the same
        thread_id to aresume_query to continue after answering."""
        async for item in self._astream_graph({"messages": [HumanMessage(content=query)]}, thread_id):
            yield item

    async def aresume_query(self, answer: str, thread_id: str):
        """Resumes a run previously paused by the ask_user tool."""
        async for item in self._astream_graph(Command(resume=answer), thread_id):
            yield item

    async def _astream_graph(self, input_value, thread_id: str):
        graph = await self._ensure_graph()
        config = {"configurable": {"thread_id": thread_id}, "callbacks": get_langchain_callbacks()}
        final_text = ""
        interrupted_question: str | None = None
        async for chunk in graph.astream(input_value, stream_mode="updates", config=config):
            for node_name, node_output in (chunk or {}).items():
                if node_name == "__interrupt__":
                    interrupted_question = node_output[0].value if node_output else "More information needed."
                    continue
                messages = node_output.get("messages", []) if isinstance(node_output, dict) else []
                for message in messages:
                    text = getattr(message, "content", "") or ""
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
