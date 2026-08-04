import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Part, TaskState, TextPart
from a2a.utils import new_task
from fastapi import FastAPI

from .a2a_peer_client import get_incoming_call_depth
from .a2a_queue_workaround import ResilientQueueManager
from .a2a_tracing import init_tracing, instrument_app
from .a2a_utils import get_original_user_text
from .orchestrator_agent import OrchestratorAgent
from .settings import ORCHESTRATOR_HOST, ORCHESTRATOR_PORT, ORCHESTRATOR_URL

_PROGRESS_PREVIEW_CHARS = 300


class OrchestratorExecutor(AgentExecutor):
    def __init__(self) -> None:
        self.agent = OrchestratorAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text = get_original_user_text(context)
        call_depth = get_incoming_call_depth(context.message.metadata if context.message else None)
        task = context.current_task
        is_resuming = bool(task) and task.status.state == TaskState.input_required

        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        source = f"mesh call at depth {call_depth}" if call_depth else "top-level call"
        print(
            f"[orchestrator] {'resuming' if is_resuming else 'new'} task={task.id} "
            f"({source}): {text[:100]!r}"
        )

        # The A2A task ID doubles as the LangGraph thread ID, so resuming a
        # paused task continues the same checkpointed graph state - no
        # ResumePendingInputMiddleware-style translation needed, unlike the
        # ADK orchestrator.
        if is_resuming:
            stream = self.agent.aresume_route(text, thread_id=task.id, call_depth=call_depth)
        else:
            stream = self.agent.astream_route(text, thread_id=task.id, call_depth=call_depth)

        result_kind = "__final__"
        result_text = ""
        async for kind, chunk_text in stream:
            if kind in ("__final__", "input_required"):
                result_kind = kind
                result_text = chunk_text
                continue
            preview = (
                chunk_text
                if len(chunk_text) <= _PROGRESS_PREVIEW_CHARS
                else chunk_text[:_PROGRESS_PREVIEW_CHARS] + "…"
            )
            await updater.update_status(
                TaskState.working,
                message=updater.new_agent_message([Part(root=TextPart(text=f"[{kind}] {preview}"))]),
            )

        print(f"[orchestrator] task={task.id} finished as {result_kind}")

        if result_kind == "input_required":
            await updater.requires_input(
                message=updater.new_agent_message([Part(root=TextPart(text=result_text))]),
            )
        else:
            await updater.complete(
                message=updater.new_agent_message([Part(root=TextPart(text=result_text))]),
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("The orchestrator does not support cancelling in-flight requests.")


def build_agent_card() -> AgentCard:
    return AgentCard(
        name="orchestrator",
        description=(
            "Routes a request to exactly one of coding_agent, research_agent, "
            "or weather_agent via a structured-output routing decision, then "
            "relays that sub-agent's response as-is. LangGraph-native "
            "alternative to the ADK orchestrator (orchestrator_server.py) - "
            "see ORCHESTRATOR_BACKEND in settings.py."
        ),
        url=ORCHESTRATOR_URL,
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[
            AgentSkill(
                id="route_request",
                name="Route a request",
                description=(
                    "Picks the single most relevant sub-agent for a request and "
                    "delegates to it, bubbling up any clarifying question the "
                    "chosen sub-agent needs answered."
                ),
                tags=["orchestrator", "routing", "langgraph", "ollama"],
            )
        ],
    )


def create_app() -> FastAPI:
    init_tracing(service_name="orchestrator_langgraph_a2a")
    agent_card = build_agent_card()
    handler = DefaultRequestHandler(
        agent_executor=OrchestratorExecutor(),
        task_store=InMemoryTaskStore(),
        queue_manager=ResilientQueueManager(),
    )
    app = A2AFastAPIApplication(agent_card=agent_card, http_handler=handler).build()
    instrument_app(app)
    return app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host=ORCHESTRATOR_HOST, port=ORCHESTRATOR_PORT, log_level="info")


if __name__ == "__main__":
    main()
