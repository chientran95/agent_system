from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.models.lite_llm import LiteLlm

from agent_system.settings import CODE_AGENT_URL, LITELLM_OLLAMA_MODEL, RESEARCH_AGENT_URL

coding_agent = RemoteA2aAgent(
    name="coding_agent",
    agent_card=f"{CODE_AGENT_URL}/.well-known/agent-card.json",
    description="Generates and edits code using an Anthropic model via the Claude Agent SDK.",
)

research_agent = RemoteA2aAgent(
    name="research_agent",
    agent_card=f"{RESEARCH_AGENT_URL}/.well-known/agent-card.json",
    description=(
        "Researches a topic using web search, then delegates to an internal "
        "content-writing subagent to produce a publish-ready blog post."
    ),
)

root_agent = Agent(
    name="orchestrator",
    model=LiteLlm(model=LITELLM_OLLAMA_MODEL),
    instruction=(
        "You are the routing orchestrator for a multi-agent system. "
        "Delegate any request about writing, editing, reviewing, or explaining code "
        "to coding_agent. Delegate any request that asks you to research a topic, or "
        "to write, verify, or research content for a blog post or article, to "
        "research_agent. Always delegate to exactly one sub-agent per request, then "
        "return its response as-is."
    ),
    sub_agents=[coding_agent, research_agent],
)
