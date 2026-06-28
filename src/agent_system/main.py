import uvicorn
from .adk_orchestrator import create_app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
