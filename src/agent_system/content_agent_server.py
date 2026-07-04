import json
import uuid

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
from .content_agent import ContentAgent
from .settings import CONTENT_AGENT_HOST, CONTENT_AGENT_PORT, CONTENT_AGENT_URL


class ContentAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self.agent = ContentAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        payload = get_original_user_text(context)
        brief, filename = _parse_request(payload)

        draft_path = self.agent.write_draft(brief, filename)
        verification = self.agent.verify_draft(brief, self.agent.get_draft(filename))

        result = json.dumps({"draft_path": str(draft_path), "verification": verification})
        await event_queue.enqueue_event(
            new_agent_text_message(result, context_id=context.context_id, task_id=context.task_id)
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("The content agent does not support cancelling in-flight requests.")


def _parse_request(payload: str) -> tuple[str, str]:
    """Accept either a plain brief string or a {"brief", "filename"} JSON payload."""
    default_filename = f"draft-{uuid.uuid4().hex[:8]}.md"
    try:
        data = json.loads(payload)
        if isinstance(data, dict) and "brief" in data:
            return data["brief"], data.get("filename", default_filename)
    except (json.JSONDecodeError, TypeError):
        pass
    return payload, default_filename


def build_agent_card() -> AgentCard:
    return AgentCard(
        name="content_agent",
        description=(
            "Writes news-style article drafts from a brief using LangChain Deep "
            "Agents backed by a local Ollama model, and verifies them against a rubric."
        ),
        url=CONTENT_AGENT_URL,
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=False),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[
            AgentSkill(
                id="write_draft",
                name="Write article draft",
                description="Writes and verifies a news-style draft from a brief.",
                tags=["content", "writing", "ollama"],
            )
        ],
    )


def create_app() -> FastAPI:
    init_tracing(service_name="content_agent_a2a")
    agent_card = build_agent_card()
    handler = DefaultRequestHandler(
        agent_executor=ContentAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    app = A2AFastAPIApplication(agent_card=agent_card, http_handler=handler).build()
    instrument_app(app)
    return app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host=CONTENT_AGENT_HOST, port=CONTENT_AGENT_PORT, log_level="info")


if __name__ == "__main__":
    main()
