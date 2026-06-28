import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = Path(os.getenv("AGENT_STORAGE_DIR", ROOT / "storage"))
CHECKPOINT_DB = Path(os.getenv("AGENT_CHECKPOINT_DB", ROOT / "state" / "checkpoint.sqlite"))
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-3-5")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
ADK_DEV_UI = os.getenv("ADK_DEV_UI", "http://localhost:3000")

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
