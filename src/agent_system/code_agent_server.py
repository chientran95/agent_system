import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message
from fastapi import FastAPI

from .a2a_tracing import init_tracing, instrument_app
from .a2a_utils import get_original_user_text
from .code_agent import CodeAgent
from .settings import CODE_AGENT_HOST, CODE_AGENT_PORT, CODE_AGENT_URL


class CodeAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self.agent = CodeAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        prompt = get_original_user_text(context)
        result = await self.agent.generate_code(prompt)
        await event_queue.enqueue_event(
            new_agent_text_message(result, context_id=context.context_id, task_id=context.task_id)
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("The coding agent does not support cancelling in-flight requests.")


def build_agent_card() -> AgentCard:
    return AgentCard(
        name="coding_agent",
        description=(
            "Generates and edits code from natural-language prompts using the "
            "Claude Agent SDK backed by an Anthropic model."
        ),
        url=CODE_AGENT_URL,
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=False),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[
            AgentSkill(
                id="generate_code",
                name="Generate code",
                description="Writes or edits code for a given natural-language prompt.",
                tags=["code", "anthropic"],
            )
        ],
    )


def create_app() -> FastAPI:
    init_tracing(service_name="code_agent_a2a")
    agent_card = build_agent_card()
    handler = DefaultRequestHandler(
        agent_executor=CodeAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    app = A2AFastAPIApplication(agent_card=agent_card, http_handler=handler).build()
    instrument_app(app)
    return app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host=CODE_AGENT_HOST, port=CODE_AGENT_PORT, log_level="info")


if __name__ == "__main__":
    main()
