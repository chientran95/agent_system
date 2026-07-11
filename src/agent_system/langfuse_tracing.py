from collections.abc import Callable
from typing import TypeVar

from .settings import LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY

LANGFUSE_CONFIGURED = bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)

F = TypeVar("F", bound=Callable)


def get_langchain_callbacks() -> list:
    """Returns a Langfuse callback for LangChain/LangGraph .invoke()/.astream()
    calls (config={"callbacks": ...}), or [] if Langfuse isn't configured.
    Attaching this at a top-level graph call also traces any nested
    subagent graphs invoked underneath it, since LangChain propagates
    callbacks down through nested Runnable calls automatically."""
    if not LANGFUSE_CONFIGURED:
        return []

    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]


def trace_generation(name: str) -> Callable[[F], F]:
    """Wraps a raw (non-LangChain) LLM-calling function - e.g. a direct
    litellm.completion() call - as a Langfuse "generation" span. No-op if
    Langfuse isn't configured.

    litellm has a built-in "langfuse" callback, but it's written against the
    old Langfuse v2 SDK (langfuse.version.__version__, removed in v4) - and
    v2 itself doesn't work with our modern LangChain (langchain.callbacks.base
    was removed in LangChain 1.x). This decorator avoids that broken bridge
    entirely by using Langfuse's own generic instrumentation.
    """
    if not LANGFUSE_CONFIGURED:
        return lambda func: func

    from langfuse import observe

    return observe(name=name, as_type="generation")
