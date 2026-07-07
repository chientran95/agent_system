import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = Path(os.getenv("AGENT_STORAGE_DIR", ROOT / "storage"))
CHECKPOINT_DB = Path(os.getenv("AGENT_CHECKPOINT_DB", ROOT / "state" / "checkpoint.sqlite"))

# Coding agent: Claude Agent SDK against a real Anthropic model/key.
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")

# Orchestrator + research/content agents: local Ollama model. Note the two libraries
# expect different provider-prefix conventions for the same model name:
# litellm (used directly, and via ADK's LiteLlm) wants "ollama_chat/<model>",
# while langchain's init_chat_model (used by deepagents) wants "ollama:<model>".
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
LITELLM_OLLAMA_MODEL = f"ollama_chat/{OLLAMA_MODEL}"
LANGCHAIN_OLLAMA_MODEL = f"ollama:{OLLAMA_MODEL}"

LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
ADK_DEV_UI = os.getenv("ADK_DEV_UI", "http://localhost:3000")

CODE_AGENT_HOST = os.getenv("CODE_AGENT_HOST", "localhost")
CODE_AGENT_PORT = int(os.getenv("CODE_AGENT_PORT", "8001"))
CODE_AGENT_URL = os.getenv("CODE_AGENT_URL", f"http://{CODE_AGENT_HOST}:{CODE_AGENT_PORT}")

RESEARCH_AGENT_HOST = os.getenv("RESEARCH_AGENT_HOST", "localhost")
RESEARCH_AGENT_PORT = int(os.getenv("RESEARCH_AGENT_PORT", "8002"))
RESEARCH_AGENT_URL = os.getenv(
    "RESEARCH_AGENT_URL", f"http://{RESEARCH_AGENT_HOST}:{RESEARCH_AGENT_PORT}"
)

ORCHESTRATOR_HOST = os.getenv("ORCHESTRATOR_HOST", "0.0.0.0")
ORCHESTRATOR_PORT = int(os.getenv("ORCHESTRATOR_PORT", "8000"))

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
