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


def resolve_agent_url(name: str, fallback_url: str) -> str:
    """Resolves an agent's base URL, logging which discovery mechanism was
    used: the curated registry (agent_registry.json) first, falling back to
    Direct Configuration (settings.py env vars) if the agent isn't listed
    there. Shared by both discovery sites - the mesh's call_peer_agent_by_name
    and the orchestrator's RemoteA2aAgent construction - so they log
    identically."""
    registry_url = lookup_agent_url(name)
    if registry_url:
        print(f"[agent discovery] '{name}' -> curated registry -> {registry_url}")
        return registry_url
    print(f"[agent discovery] '{name}' -> not in curated registry, falling back to direct config -> {fallback_url}")
    return fallback_url
