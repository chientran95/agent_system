import json
from pathlib import Path

_REGISTRY_PATH = Path(__file__).parent / "agent_registry.json"

_registry: dict[str, dict] | None = None


def _load_registry() -> dict[str, dict]:
    global _registry
    if _registry is None:
        try:
            _registry = json.loads(_REGISTRY_PATH.read_text())
        except FileNotFoundError:
            _registry = {}
    return _registry


def lookup_agent_url(name: str) -> str | None:
    """Looks up an agent's base URL in the curated registry (agent_registry.json)
    by its AgentCard name. Returns None if the agent isn't listed there, so
    callers can fall back to Direct Configuration (settings.py env vars)."""
    entry = _load_registry().get(name)
    return entry.get("url") if entry else None


def list_agents() -> dict[str, dict]:
    """Returns the full curated registry, keyed by AgentCard name."""
    return dict(_load_registry())
