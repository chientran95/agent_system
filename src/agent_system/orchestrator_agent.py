from typing import Annotated, Any, Literal, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, Interrupt, Send, interrupt
from pydantic import BaseModel, Field

from .a2a_peer_client import PeerInputRequired, call_peer_agent_by_name
from .langfuse_tracing import get_langchain_callbacks
from .settings import CLAUDE_API_KEY, CODE_AGENT_URL, ORCHESTRATOR_AGENT_MODEL, RESEARCH_AGENT_URL, WEATHER_AGENT_URL

PLANNING_SYSTEM_PROMPT = (
    "You are the planning step for a multi-agent system with three worker "
    "agents:\n"
    "- coding_agent: writes/edits/reviews code.\n"
    "- research_agent: researches a topic via web search, can write blog/article content.\n"
    "- weather_agent: answers weather, forecast, air quality, or climate questions.\n\n"
    "Break the user's request into a plan: an ordered list of waves. Steps "
    "within the same wave run in PARALLEL and must be independent of each "
    "other (no step in a wave may need another step in the SAME wave's "
    "output). If a step needs a result from an earlier step, put it in a "
    "LATER wave - reference what it needs in plain English in the task "
    "text (e.g. 'using the research findings above'), you don't need any "
    "special syntax. Use as few waves and steps as the request actually "
    "needs - a simple single-agent request should be a single wave with a "
    "single step, not artificially split up."
)

SUMMARIZE_SYSTEM_PROMPT = (
    "You are combining the results of one or more delegated steps into a "
    "single final answer for the user. Return the combined answer directly "
    "- do not mention the plan, the steps, or the agents involved unless "
    "the user's own request asked about process. If there's only one step "
    "result, return it unchanged rather than paraphrasing it."
)

# Safety cap on the "peer paused, fold the answer back into the request text,
# retry" loop in dispatch_step - same pattern and same reasoning as
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


class PlanStep(BaseModel):
    step_id: str = Field(description="Short unique id, e.g. 'step_1'.")
    agent_name: Literal["coding_agent", "research_agent", "weather_agent"]
    task: str = Field(description="Self-contained instruction for that agent.")


class Wave(BaseModel):
    steps: list[PlanStep]


class Plan(BaseModel):
    """Structured output for the planning step - see docs discussion on
    "waves": an ordered list of parallel-safe step batches, rather than a
    general dependency graph, since that's a much more reliably-generatable
    structured-output target (confirmed via a standalone spike across
    single-agent, independent-parallel, and sequential-dependency cases)."""

    waves: list[Wave]


class StepResult(TypedDict):
    step_id: str
    agent_name: str
    text: str


def _merge_step_results(existing: dict[str, StepResult], new: dict[str, StepResult]) -> dict[str, StepResult]:
    return {**existing, **new}


class OrchestratorState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    # plain dict (Plan.model_dump()), not the Pydantic object - the
    # checkpointer serializes state after every step, and (same lesson as
    # weather_agent.py's WeatherState/StructuredTool bug) arbitrary Pydantic
    # objects aren't a supported msgpack type long-term.
    plan: dict[str, Any] | None
    current_wave_index: int
    # keyed by step_id, merged via reducer since parallel dispatch_step
    # calls in the same wave each contribute independently
    step_results: Annotated[dict[str, StepResult], _merge_step_results]


class DispatchInput(TypedDict):
    """Payload for one Send()-mapped dispatch_step invocation - deliberately
    narrower than OrchestratorState, same split verified safe in the
    interrupt-under-fan-out spike (completed sibling branches' results
    survive a pause elsewhere in the same wave; only the interrupted branch
    re-runs on resume)."""

    step: dict[str, Any]  # plain dict (PlanStep.model_dump()), see OrchestratorState.plan
    step_results: Annotated[dict[str, StepResult], _merge_step_results]


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


def _format_step_results(step_results: dict[str, StepResult]) -> str:
    return "\n\n".join(f"[{r['step_id']}/{r['agent_name']}]: {r['text']}" for r in step_results.values())


class OrchestratorAgent:
    def __init__(self) -> None:
        self.model = ChatAnthropic(model=ORCHESTRATOR_AGENT_MODEL, api_key=CLAUDE_API_KEY)
        self.planner = self.model.with_structured_output(Plan)
        self.checkpointer = InMemorySaver()
        # thread_id -> the id of the specific pending Interrupt the last
        # input-required pause surfaced. A2A callers only ever send back
        # plain text + taskId, never an interrupt id, so this has to be
        # tracked server-side - same shape as code_agent_server.py's
        # task.id -> Claude session_id map. Only needed because a wave can
        # have multiple simultaneous pauses (see the fan-out spike); resolved
        # one at a time, resuming one correctly re-surfaces any others still
        # pending, so no bundling/parsing of multi-part answers is needed.
        self._pending_interrupt_ids: dict[str, str] = {}
        self._graph = self._build_graph()
        print(f"OrchestratorAgent (langgraph) initialized with LLM=anthropic:{ORCHESTRATOR_AGENT_MODEL}")

    def _build_graph(self):
        async def plan_node(state: OrchestratorState) -> dict[str, Any]:
            try:
                plan = await self.planner.ainvoke(
                    [SystemMessage(content=PLANNING_SYSTEM_PROMPT), *state["messages"]]
                )
                if not plan.waves:
                    raise ValueError("empty plan")
            except Exception:
                # Same fallback spirit as the old single-route design's
                # exception handler - don't leave the graph with no plan.
                request_text = _content_to_text(state["messages"][-1].content)
                plan = Plan(waves=[Wave(steps=[PlanStep(step_id="step_1", agent_name="research_agent", task=request_text)])])
            summary = "; ".join(
                f"wave {i}: [{', '.join(s.agent_name for s in wave.steps)}]" for i, wave in enumerate(plan.waves)
            )
            return {
                "plan": plan.model_dump(),
                "current_wave_index": 0,
                "step_results": {},
                "messages": [AIMessage(content=f"Planned {len(plan.waves)} wave(s) - {summary}")],
            }

        def wave_dispatch(state: OrchestratorState) -> list[Send]:
            wave = state["plan"]["waves"][state["current_wave_index"]]
            return [
                Send("dispatch_step", {"step": step, "step_results": state["step_results"]})
                for step in wave["steps"]
            ]

        async def dispatch_step(state: DispatchInput, config: RunnableConfig) -> dict[str, Any]:
            step = state["step"]
            step_id, agent_name, task = step["step_id"], step["agent_name"], step["task"]
            call_depth = (config.get("configurable") or {}).get("call_depth", 0)
            prior_context = _format_step_results(state["step_results"])
            request = f"{task}\n\nContext from prior steps:\n{prior_context}" if prior_context else task
            url = _AGENT_URLS[agent_name]
            for _ in range(_MAX_DISPATCH_RETRIES):
                try:
                    text = await call_peer_agent_by_name(agent_name, url, request, call_depth)
                    break
                except PeerInputRequired as e:
                    clarification = interrupt(e.question)
                    request = f"{request}\n\nAdditional info: {clarification}"
            else:
                raise RuntimeError(f"{agent_name} needed too many rounds of clarification.")
            return {
                "step_results": {step_id: {"step_id": step_id, "agent_name": agent_name, "text": text}},
                "messages": [AIMessage(content=f"[{step_id}/{agent_name}] {text}")],
            }

        def wave_barrier(state: OrchestratorState) -> dict[str, Any]:
            return {"current_wave_index": state["current_wave_index"] + 1}

        def after_wave(state: OrchestratorState):
            if state["current_wave_index"] < len(state["plan"]["waves"]):
                return wave_dispatch(state)  # more waves - fan out the next one
            return "summarize"  # done - reduce

        async def summarize_node(state: OrchestratorState) -> dict[str, Any]:
            results_text = _format_step_results(state["step_results"])
            if len(state["step_results"]) == 1:
                # Single-step plan (the common case) - the old design's
                # single-route behavior, no need to pay for a paraphrase call.
                summary_text = next(iter(state["step_results"].values()))["text"]
            else:
                response = await self.model.ainvoke(
                    [SystemMessage(content=SUMMARIZE_SYSTEM_PROMPT), HumanMessage(content=results_text)]
                )
                summary_text = _content_to_text(response.content)
            return {"messages": [AIMessage(content=summary_text)]}

        graph = StateGraph(OrchestratorState)
        graph.add_node("plan", plan_node)
        graph.add_node("dispatch_step", dispatch_step)
        graph.add_node("wave_barrier", wave_barrier)
        graph.add_node("summarize", summarize_node)
        graph.set_entry_point("plan")
        graph.add_conditional_edges("plan", wave_dispatch, ["dispatch_step"])
        graph.add_edge("dispatch_step", "wave_barrier")
        graph.add_conditional_edges("wave_barrier", after_wave, ["dispatch_step", "summarize"])
        graph.add_edge("summarize", END)
        return graph.compile(checkpointer=self.checkpointer)

    async def astream_route(self, request: str, thread_id: str, call_depth: int = 0):
        """Starts a new run, streaming (node_name, text) progress chunks -
        "plan" for the generated plan, "dispatch_step" per completed worker
        step, "summarize" for the final combined answer. The final yield is
        either ("__final__", answer) on completion, or ("input_required",
        question) if a worker paused - one question at a time, even if
        multiple steps in a wave pause simultaneously. Pass the same
        thread_id to aresume_route to continue."""
        async for item in self._astream_graph(
            {"messages": [HumanMessage(content=request)]}, thread_id, call_depth
        ):
            yield item

    async def aresume_route(self, answer: str, thread_id: str, call_depth: int = 0):
        """Resumes a run previously paused (input_required)."""
        interrupt_id = self._pending_interrupt_ids.get(thread_id)
        resume_value = {interrupt_id: answer} if interrupt_id else answer
        async for item in self._astream_graph(Command(resume=resume_value), thread_id, call_depth):
            yield item

    async def _astream_graph(self, input_value, thread_id: str, call_depth: int):
        config = {
            "configurable": {"thread_id": thread_id, "call_depth": call_depth},
            "callbacks": get_langchain_callbacks(),
        }
        final_text = ""
        interrupted: Interrupt | None = None
        async for chunk in self._graph.astream(input_value, stream_mode="updates", config=config):
            for node_name, node_output in (chunk or {}).items():
                if node_name == "__interrupt__":
                    # Only the first is surfaced per round trip - resuming it
                    # correctly re-surfaces any others still pending on the
                    # next call (verified in the fan-out spike), so pauses
                    # are resolved one at a time rather than bundled.
                    interrupted = node_output[0] if node_output else None
                    continue
                messages = node_output.get("messages", []) if isinstance(node_output, dict) else []
                for message in messages:
                    text = _content_to_text(getattr(message, "content", None))
                    if not text:
                        continue
                    if getattr(message, "type", None) == "ai":
                        final_text = text
                    yield node_name, text
        if interrupted is not None:
            self._pending_interrupt_ids[thread_id] = interrupted.id
            yield "input_required", interrupted.value
        else:
            self._pending_interrupt_ids.pop(thread_id, None)
            yield "__final__", final_text
