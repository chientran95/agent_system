import uuid
from typing import Any

import httpx

from .agent_registry import resolve_agent_url

MAX_MESH_CALL_DEPTH = 10
MESH_DEPTH_METADATA_KEY = "mesh_call_depth"


class MeshDepthExceeded(Exception):
    """Raised when a peer-agent call would exceed MAX_MESH_CALL_DEPTH."""


class PeerInputRequired(Exception):
    """Raised by call_peer_agent when the callee paused (input-required)
    instead of completing, so the caller can bubble the pause upward - e.g.
    by calling LangGraph's interrupt(), or by relaying the question back to
    its own model to ask via its own ask_user tool."""

    def __init__(self, question: str, peer_url: str) -> None:
        self.question = question
        self.peer_url = peer_url
        super().__init__(question)


async def call_peer_agent(url: str, text: str, call_depth: int) -> str:
    """Calls another agent's A2A server with a single message and returns its
    final text response. Used for direct peer-to-peer delegation (a mesh
    call), as opposed to going through the orchestrator.

    call_depth is the depth of the CALLER (0 for a top-level request); this
    increments it by one and refuses to place the call if that would exceed
    MAX_MESH_CALL_DEPTH, to guard against call cycles between agents.
    """
    next_depth = call_depth + 1
    if next_depth > MAX_MESH_CALL_DEPTH:
        raise MeshDepthExceeded(
            f"Refusing to call {url}: mesh call depth would exceed {MAX_MESH_CALL_DEPTH}."
        )

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "messageId": str(uuid.uuid4()),
                "metadata": {MESH_DEPTH_METADATA_KEY: next_depth},
            }
        },
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=300.0)
    response.raise_for_status()
    body = response.json()

    if "error" in body:
        raise RuntimeError(f"Peer agent {url} returned an error: {body['error']}")

    result = body.get("result") or {}
    status = result.get("status") or {}
    if status.get("state") == "input-required":
        raise PeerInputRequired(_text_from_message(status.get("message")), url)

    return _extract_text(result)


async def call_peer_agent_by_name(name: str, fallback_url: str, text: str, call_depth: int) -> str:
    """Resolves the callee's URL via the curated registry first (by
    AgentCard name, see agent_registry.py); falls back to fallback_url
    (Direct Configuration, e.g. a *_AGENT_URL from settings.py) if the
    agent isn't listed in the registry. Both discovery strategies are wired
    up side by side this way - editing agent_registry.json changes routing
    with no code changes, but nothing breaks if an agent isn't registered."""
    url = resolve_agent_url(name, fallback_url)
    return await call_peer_agent(url, text, call_depth)


def get_incoming_call_depth(message_metadata: dict[str, Any] | None) -> int:
    """Reads the mesh call depth from an incoming A2A message's metadata.
    0 means this is a top-level request (e.g. from the orchestrator or a
    direct caller), not part of a mesh chain."""
    if not message_metadata:
        return 0
    return int(message_metadata.get(MESH_DEPTH_METADATA_KEY, 0))


def _extract_text(result: dict[str, Any]) -> str:
    """Pulls the final answer text out of a completed task result, regardless
    of which of our agents' response shape produced it: some put the final
    text on status.message (e.g. weather_agent's completed messages), others
    put it in an artifact (code_agent, research_agent). Falls back to the
    last agent-authored history entry. Callers should check for the
    input-required state (raised as PeerInputRequired) before calling this."""
    status = result.get("status") or {}
    artifacts = result.get("artifacts") or []
    if artifacts:
        text = _text_from_parts(artifacts[-1].get("parts"))
        if text:
            return text

    message_text = _text_from_message(status.get("message"))
    if message_text:
        return message_text

    for event in reversed(result.get("history") or []):
        if event.get("role") == "agent":
            text = _text_from_parts(event.get("parts"))
            if text:
                return text

    return ""


def _text_from_message(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    return _text_from_parts(message.get("parts"))


def _text_from_parts(parts: list[dict[str, Any]] | None) -> str:
    if not parts:
        return ""
    return "\n".join(p["text"] for p in parts if "text" in p)
