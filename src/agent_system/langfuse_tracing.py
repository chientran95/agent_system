from .settings import LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY

LANGFUSE_CONFIGURED = bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)


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


def configure_litellm() -> None:
    """Enables litellm's built-in Langfuse callback, for raw
    litellm.completion() calls that don't go through a LangChain Runnable
    (e.g. ContentAgent.verify_draft's rubric check). No-op if unconfigured."""
    if not LANGFUSE_CONFIGURED:
        return

    import litellm

    litellm.success_callback = ["langfuse"]
    litellm.failure_callback = ["langfuse"]
