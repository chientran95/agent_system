from contextlib import AsyncExitStack
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from .langfuse_tracing import get_langchain_callbacks
from .settings import NVIDIA_API_KEY, WEATHER_AGENT_MODEL

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

# geocoding is needed by almost every query (forecast tools take lat/lon, not
# place names) and its schema is tiny, so it's always bound rather than left
# to the first-pass selector to remember.
_ALWAYS_INCLUDED_TOOL_NAMES = {"geocoding"}
_MAX_SELECTED_TOOLS = 3

TOOL_SELECTOR_SYSTEM_PROMPT = (
    "You are choosing which weather-data tools a downstream agent will need "
    "to answer the user's request. You are shown each tool's name and "
    "description only (not its full parameters). geocoding is always "
    "available separately, so don't worry about picking it here - focus "
    "only on which weather-data tool(s) are actually needed.\n\n"
    f"Pick at most {_MAX_SELECTED_TOOLS} tools - usually just one is enough. "
    "Choose exact names only from the list below."
)


class ToolSelection(BaseModel):
    """Structured output for the cheap first-pass tool-selection call."""

    tool_names: list[str] = Field(
        description=(
            f"Names of up to {_MAX_SELECTED_TOOLS} tools, chosen only from the "
            "provided list, most relevant to the user's request."
        )
    )


class WeatherState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    # Tool names only, not BaseTool objects - the checkpointer serializes
    # state after every step, and StructuredTool instances aren't
    # msgpack-serializable.
    selected_tool_names: list[str]


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
        self.model = ChatNVIDIA(
            model=WEATHER_AGENT_MODEL,
            api_key=NVIDIA_API_KEY,
            temperature=1,
            top_p=1,
            max_tokens=16384,
            seed=42,
        )
        self.checkpointer = InMemorySaver()
        self._graph = None
        self._exit_stack = AsyncExitStack()
        print(f"WeatherAgent initialized with LLM=nvidia:{WEATHER_AGENT_MODEL}")

    async def _ensure_graph(self):
        """Builds the graph lazily, since loading MCP tools is async and we
        don't want to block __init__."""
        if self._graph is not None:
            return self._graph

        # Open ONE persistent MCP session for the agent's whole lifetime,
        # instead of get_tools()'s default behavior of spinning up a fresh
        # session (and, for stdio, a fresh open-meteo-mcp-server subprocess)
        # for every single tool call. That per-call respawn caused
        # intermittent tool failures with a blank error message - a cold-start
        # race between the freshly-spawned process and its first real API
        # call to Open-Meteo. See docs/BUG_WEATHER_TOOL_SELECTION.md.
        session = await self._exit_stack.enter_async_context(self.mcp_client.session("open_meteo"))
        mcp_tools = await load_mcp_tools(session)
        always_included = [t for t in mcp_tools if t.name in _ALWAYS_INCLUDED_TOOL_NAMES]
        selectable = [t for t in mcp_tools if t.name not in _ALWAYS_INCLUDED_TOOL_NAMES]
        selectable_by_name = {t.name: t for t in selectable}
        selector_model = self.model.with_structured_output(ToolSelection)

        async def select_tools_node(state: WeatherState) -> dict[str, Any]:
            """Cheap first pass: shows only tool name+description (not full
            parameter schemas) so even a small-context model can see every
            candidate at once, then narrows to a handful of tools whose full
            (much larger) schemas get bound for the actual agent turn below.
            See docs/BUG_WEATHER_TOOL_SELECTION.md for why this exists."""
            tool_list_text = "\n".join(f"- {t.name}: {t.description}" for t in selectable)
            try:
                selection = await selector_model.ainvoke(
                    [
                        SystemMessage(content=f"{TOOL_SELECTOR_SYSTEM_PROMPT}\n\n{tool_list_text}"),
                        *state["messages"],
                    ]
                )
                selected_names = [
                    name for name in dict.fromkeys(selection.tool_names) if name in selectable_by_name
                ][:_MAX_SELECTED_TOOLS]
            except Exception:
                selected_names = []
            if not selected_names:
                # Selector produced nothing usable - fall back to the
                # general-purpose tool rather than leaving the agent with no
                # weather-data tool at all.
                selected_names = ["weather_forecast"] if "weather_forecast" in selectable_by_name else []
            return {"selected_tool_names": selected_names}

        async def agent_node(state: WeatherState) -> dict[str, Any]:
            selected = [
                selectable_by_name[name]
                for name in state.get("selected_tool_names", [])
                if name in selectable_by_name
            ]
            tools_for_turn = [*always_included, *selected, ask_user]
            model_with_tools = self.model.bind_tools(tools_for_turn)
            response = await model_with_tools.ainvoke(
                [SystemMessage(content=WEATHER_SYSTEM_PROMPT), *state["messages"]]
            )
            return {"messages": [response]}

        graph = StateGraph(WeatherState)
        graph.add_node("select_tools", select_tools_node)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", ToolNode([*mcp_tools, ask_user]))
        graph.set_entry_point("select_tools")
        graph.add_edge("select_tools", "agent")
        graph.add_conditional_edges("agent", tools_condition)
        graph.add_edge("tools", "agent")
        self._graph = graph.compile(checkpointer=self.checkpointer)
        return self._graph

    async def aclose(self) -> None:
        """Closes the persistent MCP session opened by _ensure_graph."""
        await self._exit_stack.aclose()

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
