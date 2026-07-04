from pathlib import Path

import uvicorn
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app

from .a2a_tracing import init_tracing, instrument_app
from .settings import ORCHESTRATOR_HOST, ORCHESTRATOR_PORT

AGENTS_DIR = str(Path(__file__).resolve().parent / "orchestrator_agents")


def create_app() -> FastAPI:
    init_tracing(service_name="orchestrator")
    app = get_fast_api_app(agents_dir=AGENTS_DIR, web=True)
    instrument_app(app)
    return app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host=ORCHESTRATOR_HOST, port=ORCHESTRATOR_PORT, log_level="info")


if __name__ == "__main__":
    main()
