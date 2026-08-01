from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.models.lite_llm import LiteLlm

from agent_system.agent_registry import resolve_agent_url
from agent_system.settings import (
    CODE_AGENT_URL,
    LITELLM_ORCHESTRATOR_MODEL,
    RESEARCH_AGENT_URL,
    WEATHER_AGENT_URL,
)


def _agent_card_url(name: str, fallback_url: str) -> str:
    """Resolves an agent's base URL via the curated registry first (by
    AgentCard name), falling back to Direct Configuration (settings.py) if
    the agent isn't listed in the registry - the same discovery precedence
    used by the mesh's call_peer_agent_by_name."""
    url = resolve_agent_url(name, fallback_url)
    return f"{url}/.well-known/agent-card.json"


coding_agent = RemoteA2aAgent(
    name="coding_agent",
    agent_card=_agent_card_url("coding_agent", CODE_AGENT_URL),
    description="Generates and edits code using an Anthropic model via the Claude Agent SDK.",
)

research_agent = RemoteA2aAgent(
    name="research_agent",
    agent_card=_agent_card_url("research_agent", RESEARCH_AGENT_URL),
    description=(
        "Researches a topic using web search, then delegates to an internal "
        "content-writing subagent to produce a publish-ready blog post."
    ),
)

weather_agent = RemoteA2aAgent(
    name="weather_agent",
    agent_card=_agent_card_url("weather_agent", WEATHER_AGENT_URL),
    description=(
        "Answers weather, forecast, air quality, marine, and climate questions "
        "using the Open-Meteo MCP server. May ask a clarifying question if the "
        "request is missing information such as a location."
    ),
)

root_agent = Agent(
    name="orchestrator",
    model=LiteLlm(model=LITELLM_ORCHESTRATOR_MODEL),
    instruction=(
        "You are the routing orchestrator for a multi-agent system. "
        "Delegate any request about writing, editing, reviewing, or explaining code "
        "to coding_agent. Delegate any request that asks you to research a topic, or "
        "to write, verify, or research content for a blog post or article, to "
        "research_agent. Delegate any request about weather, forecasts, air quality, "
        "or climate to weather_agent. Always delegate to exactly one sub-agent per "
        "request, then return its response as-is."
    ),
    sub_agents=[coding_agent, research_agent, weather_agent],
)
