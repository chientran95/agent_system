from pathlib import Path
from fastapi import FastAPI, HTTPException
from .a2a_tracing import instrument_app
from .code_agent import CodeAgent
from .content_agent import ContentAgent
from .settings import ADK_DEV_UI


def create_app() -> FastAPI:
    app = FastAPI(title="Agent System ADK Orchestrator")
    instrument_app(app)

    code_agent = CodeAgent()
    content_agent = ContentAgent()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "adk_dev_ui": ADK_DEV_UI}

    @app.post("/dispatch/code")
    async def dispatch_code(payload: dict) -> dict:
        if "prompt" not in payload:
            raise HTTPException(status_code=400, detail="Missing prompt in payload")

        prompt = payload["prompt"]
        generated = code_agent.generate_code(prompt)
        return {"generated_code": generated}

    @app.post("/dispatch/content")
    async def dispatch_content(payload: dict) -> dict:
        if "brief" not in payload or "filename" not in payload:
            raise HTTPException(status_code=400, detail="Missing brief or filename")

        brief = payload["brief"]
        filename = payload["filename"]
        draft_path = content_agent.write_draft(brief, filename)
        verification = content_agent.verify_draft(brief, content_agent.get_draft(filename))
        return {
            "draft_path": str(draft_path),
            "verification": verification,
        }

    @app.post("/verify/code")
    async def verify_code(payload: dict) -> dict:
        if "workspace_dir" not in payload:
            raise HTTPException(status_code=400, detail="Missing workspace_dir")

        workspace_dir = payload["workspace_dir"]
        success = code_agent.mechanical_verify(Path(workspace_dir))
        return {"verified": success}

    return app
