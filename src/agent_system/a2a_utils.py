from a2a.server.agent_execution import RequestContext
from a2a.utils import get_text_parts


def get_original_user_text(context: RequestContext) -> str:
    """Return just the original user instruction from a request.

    ADK's RemoteA2aAgent relays a transfer_to_agent call as multiple text
    parts: the real instruction followed by injected "For context: ..."
    bookkeeping about the tool call itself. context.get_user_input() joins
    all of them, which pollutes single-shot prompts/briefs with that
    bookkeeping text - so take only the first part instead.
    """
    if not context.message:
        return ""
    text_parts = get_text_parts(context.message.parts)
    return text_parts[0] if text_parts else ""
