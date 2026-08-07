import uuid

from ag_ui.core import Interrupt, RunAgentInput
from ag_ui.core.events import (
    CustomEvent,
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from .orchestrator_agent import OrchestratorAgent

_REASON_CLARIFICATION_NEEDED = "clarification_needed"


def _last_user_text(input_data: RunAgentInput) -> str:
    for message in reversed(input_data.messages):
        if message.role == "user" and message.content:
            return message.content
    return ""


def add_orchestrator_agui_endpoint(app: FastAPI, orchestrator_agent: OrchestratorAgent, path: str = "/agui") -> None:
    """Hand-rolled AG-UI translation layer around OrchestratorAgent's own
    stream, deliberately NOT using the ag-ui-langgraph package's
    LangGraphAgent class. That package (0.0.42, latest as of writing) has
    two confirmed bugs under Send()-based parallel fan-out + simultaneous
    interrupt()s - verified via a standalone spike, not just read from
    source:

    1. Its live-stream interrupt collection reads only tasks[0].interrupts,
       silently dropping any second simultaneous pause from the event the
       client actually sees.
    2. Its resume path always does Command(resume=<whatever raw value the
       client sent>) - a plain scalar - which crashes with a raw,
       uncaught RuntimeError (not a clean AG-UI error event) if more than
       one interrupt happens to be pending, since LangGraph itself requires
       {interrupt_id: value} once there's more than one.

    This translator instead wraps OrchestratorAgent's own _pending_interrupts
    tracking (already correct - built and verified before this bug was ever
    found) and uses the AG-UI core protocol's own purpose-built multi-
    interrupt shape: RunFinishedEvent.outcome=RunFinishedInterruptOutcome
    (a proper `interrupts: list[Interrupt]`) and RunAgentInput.resume (a
    proper `list[ResumeEntry]`, keyed by interrupt_id) - both declared in
    ag_ui.core.types but left unused by ag-ui-langgraph's own adapter.
    See docs/BUG_AG_UI_MULTI_INTERRUPT.md for the full investigation.
    """

    @app.post(path)
    async def agui_endpoint(input_data: RunAgentInput, request: Request):
        encoder = EventEncoder(accept=request.headers.get("accept"))

        async def event_generator():
            thread_id = input_data.thread_id
            run_id = input_data.run_id
            try:
                yield encoder.encode(RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread_id, run_id=run_id))

                if input_data.resume:
                    resume_map = {entry.interrupt_id: entry.payload for entry in input_data.resume}
                    stream = orchestrator_agent.aresume_route(resume_map, thread_id=thread_id)
                else:
                    stream = orchestrator_agent.astream_route(_last_user_text(input_data), thread_id=thread_id)

                message_id = str(uuid.uuid4())
                message_started = False
                paused = False
                async for kind, text in stream:
                    if kind == "input_required":
                        # The actual pause is reported once, below, via
                        # RunFinishedInterruptOutcome - this branch only
                        # marks that it happened; get_pending_interrupts()
                        # is the single source of truth for the full list.
                        paused = True
                        continue
                    if kind == "__final__":
                        if not message_started:
                            yield encoder.encode(
                                TextMessageStartEvent(
                                    type=EventType.TEXT_MESSAGE_START, message_id=message_id, role="assistant"
                                )
                            )
                            message_started = True
                        yield encoder.encode(
                            TextMessageContentEvent(
                                type=EventType.TEXT_MESSAGE_CONTENT, message_id=message_id, delta=text
                            )
                        )
                        continue
                    # Progress chunk (plan / dispatch_step / summarize node
                    # names) - surfaced as a custom event so a UI *can* show
                    # live progress, without forcing it to.
                    yield encoder.encode(
                        CustomEvent(type=EventType.CUSTOM, name=f"orchestrator_progress_{kind}", value=text)
                    )

                if message_started:
                    yield encoder.encode(TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=message_id))

                if paused:
                    pending = orchestrator_agent.get_pending_interrupts(thread_id)
                    outcome = RunFinishedInterruptOutcome(
                        interrupts=[
                            Interrupt(id=i.id, reason=_REASON_CLARIFICATION_NEEDED, message=str(i.value))
                            for i in pending
                        ]
                    )
                else:
                    outcome = RunFinishedSuccessOutcome()

                yield encoder.encode(
                    RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=thread_id, run_id=run_id, outcome=outcome)
                )
            except Exception as e:
                # The bug this whole file exists to avoid: never let an
                # exception kill the stream with a raw dropped connection -
                # always surface it as a real AG-UI event instead.
                yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, message=str(e)))

        return StreamingResponse(event_generator(), media_type=encoder.get_content_type())
