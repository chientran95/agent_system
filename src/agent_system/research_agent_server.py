import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Part, TaskState, TextPart
from a2a.utils import new_task
from fastapi import FastAPI

from .a2a_tracing import init_tracing, instrument_app
from .a2a_utils import get_original_user_text
from .research_agent import ResearchAgent
from .settings import RESEARCH_AGENT_HOST, RESEARCH_AGENT_PORT, RESEARCH_AGENT_URL

_PROGRESS_PREVIEW_CHARS = 300


class ResearchAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self.agent = ResearchAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        topic = get_original_user_text(context)

        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        result_text = ""
        async for node_name, text in self.agent.astream_research(topic):
            if node_name == "__final__":
                result_text = text
                continue
            preview = text if len(text) <= _PROGRESS_PREVIEW_CHARS else text[:_PROGRESS_PREVIEW_CHARS] + "…"
            await updater.update_status(
                TaskState.working,
                message=updater.new_agent_message([Part(root=TextPart(text=f"[{node_name}] {preview}"))]),
            )

        await updater.add_artifact([Part(root=TextPart(text=result_text))], name="blog_post")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("The research agent does not support cancelling in-flight requests.")


def build_agent_card() -> AgentCard:
    return AgentCard(
        name="research_agent",
        description=(
            "Researches a topic using web search, then delegates to an internal "
            "content-writing subagent to produce a publish-ready blog post."
        ),
        url=RESEARCH_AGENT_URL,
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[
            AgentSkill(
                id="research_and_write",
                name="Research and write a blog post",
                description="Researches a topic via web search and returns a blog-post draft.",
                tags=["research", "content", "writing", "ollama"],
            )
        ],
    )


def create_app() -> FastAPI:
    init_tracing(service_name="research_agent_a2a")
    agent_card = build_agent_card()
    handler = DefaultRequestHandler(
        agent_executor=ResearchAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    app = A2AFastAPIApplication(agent_card=agent_card, http_handler=handler).build()
    instrument_app(app)
    return app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host=RESEARCH_AGENT_HOST, port=RESEARCH_AGENT_PORT, log_level="info")


if __name__ == "__main__":
    main()
