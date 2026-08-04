from .settings import ORCHESTRATOR_BACKEND


def main() -> None:
    """Entry point for `uv run orchestrator` - picks which orchestrator
    implementation actually runs based on ORCHESTRATOR_BACKEND (settings.py):
    "adk" (default) for the existing ADK session-REST-API orchestrator, kept
    as-is; "langgraph" for the A2A-native alternative that sidesteps
    BUG_ORCHESTRATOR_RESUME.md. Both listen on the same ORCHESTRATOR_PORT,
    just with different API shapes - see docs/curl_commands.md scenario 3
    for the ADK shape."""
    if ORCHESTRATOR_BACKEND == "langgraph":
        from .orchestrator_langgraph_server import main as run_langgraph

        run_langgraph()
    elif ORCHESTRATOR_BACKEND == "adk":
        from .orchestrator_server import main as run_adk

        run_adk()
    else:
        raise ValueError(
            f"Unknown ORCHESTRATOR_BACKEND={ORCHESTRATOR_BACKEND!r} - expected 'adk' or 'langgraph'."
        )


if __name__ == "__main__":
    main()
