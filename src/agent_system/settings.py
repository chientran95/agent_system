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

# Shared OTLP endpoint: our own OpenTelemetry pipeline (a2a_tracing.py) and
# the Claude Code CLI's native telemetry (see code_agent.py) both export here.
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

# Each of these agents (orchestrator, content_agent subagent, research_agent,
# weather_agent) has its own independently configurable model - no shared
# default, so tuning one doesn't affect the others. Note two libraries expect
# different provider-prefix conventions for the same model name: litellm
# (used directly, and via ADK's LiteLlm) wants "ollama_chat/<model>", while
# langchain's init_chat_model (used by deepagents) wants "ollama:<model>";
# ChatOllama takes the raw name.
ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "gpt-oss:20b")
LITELLM_ORCHESTRATOR_MODEL = f"ollama_chat/{ORCHESTRATOR_MODEL}"

CONTENT_AGENT_MODEL = os.getenv("CONTENT_AGENT_MODEL", "gpt-oss:20b")
LITELLM_CONTENT_AGENT_MODEL = f"ollama_chat/{CONTENT_AGENT_MODEL}"

RESEARCH_AGENT_MODEL = os.getenv("RESEARCH_AGENT_MODEL", "gpt-oss:20b")
LANGCHAIN_RESEARCH_AGENT_MODEL = f"ollama:{RESEARCH_AGENT_MODEL}"

# weather_agent runs against NVIDIA's hosted API (ChatNVIDIA) rather than
# local Ollama - see docs/BUG_WEATHER_TOOL_SELECTION.md for why local models
# struggled with this agent's tool-heavy prompts.
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
WEATHER_AGENT_MODEL = os.getenv("WEATHER_AGENT_MODEL", "z-ai/glm-5.2")

# LLM-semantic observability for the LangChain-based agents (content_agent's
# graph, research_agent's deepagents graph, and litellm's raw completion
# calls), and optionally for the orchestrator (ADK emits its own gen_ai.*
# OpenTelemetry spans natively - see a2a_tracing.py's also_export_to_langfuse).
# Complements Jaeger, which only sees the generic HTTP/A2A layer.
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
LANGFUSE_BASE_URL = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

ADK_DEV_UI = os.getenv("ADK_DEV_UI", "http://localhost:3000")

CODE_AGENT_HOST = os.getenv("CODE_AGENT_HOST", "localhost")
CODE_AGENT_PORT = int(os.getenv("CODE_AGENT_PORT", "8001"))
CODE_AGENT_URL = os.getenv("CODE_AGENT_URL", f"http://{CODE_AGENT_HOST}:{CODE_AGENT_PORT}")

RESEARCH_AGENT_HOST = os.getenv("RESEARCH_AGENT_HOST", "localhost")
RESEARCH_AGENT_PORT = int(os.getenv("RESEARCH_AGENT_PORT", "8002"))
RESEARCH_AGENT_URL = os.getenv(
    "RESEARCH_AGENT_URL", f"http://{RESEARCH_AGENT_HOST}:{RESEARCH_AGENT_PORT}"
)

WEATHER_AGENT_HOST = os.getenv("WEATHER_AGENT_HOST", "localhost")
WEATHER_AGENT_PORT = int(os.getenv("WEATHER_AGENT_PORT", "8003"))
WEATHER_AGENT_URL = os.getenv(
    "WEATHER_AGENT_URL", f"http://{WEATHER_AGENT_HOST}:{WEATHER_AGENT_PORT}"
)

ORCHESTRATOR_HOST = os.getenv("ORCHESTRATOR_HOST", "0.0.0.0")
ORCHESTRATOR_PORT = int(os.getenv("ORCHESTRATOR_PORT", "8000"))

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
