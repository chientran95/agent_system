from ddgs import DDGS
from deepagents import create_deep_agent
from langchain_core.tools import tool

from .content_agent import ContentAgent
from .settings import LANGCHAIN_OLLAMA_MODEL

RESEARCH_SYSTEM_PROMPT = (
    "You are a research agent with exactly two capabilities: the web_search "
    "tool, and a content_writer subagent. Follow this exact procedure for "
    "every request, with no exceptions:\n"
    "1. Call the web_search tool yourself, directly, one or more times, to "
    "gather facts and sources about the topic. Do not delegate this step to "
    "any subagent.\n"
    "2. Once you have enough material, call the content_writer subagent "
    "exactly once, passing it your research findings and sources as the "
    "task description.\n"
    "3. Return the content_writer subagent's blog post as your final "
    "answer, unchanged - do not summarize, rewrite, or add commentary.\n"
    "Never use the general-purpose subagent for any part of this workflow; "
    "only use web_search and content_writer as described above."
)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via DuckDuckGo and return matching results (title, url, snippet)."""
    results = DDGS().text(query, max_results=max_results)
    if not results:
        return "No results found."
    return "\n".join(
        f"- {r.get('title')}\n  {r.get('href')}\n  {r.get('body')}" for r in results
    )


class ResearchAgent:
    def __init__(self) -> None:
        self.content_agent = ContentAgent()
        self.client = create_deep_agent(
            model=LANGCHAIN_OLLAMA_MODEL,
            tools=[web_search],
            subagents=[self.content_agent.as_subagent()],
            system_prompt=RESEARCH_SYSTEM_PROMPT,
        )
        print(f"ResearchAgent initialized with LLM={LANGCHAIN_OLLAMA_MODEL}")

    def research_and_write(self, topic: str) -> str:
        print(f"ResearchAgent: researching topic:\n{topic}\n")
        result = self.client.invoke({"messages": [{"role": "user", "content": topic}]})
        return result["messages"][-1].content

    async def astream_research(self, topic: str):
        """Streams (node_name, text) progress chunks as the agent researches
        and delegates to the content subagent. The final tuple has
        node_name == "__final__" and text == the finished response."""
        final_text = ""
        async for chunk in self.client.astream(
            {"messages": [{"role": "user", "content": topic}]}, stream_mode="updates"
        ):
            for node_name, node_output in (chunk or {}).items():
                messages = node_output.get("messages", []) if isinstance(node_output, dict) else []
                for message in messages:
                    text = getattr(message, "content", "") or ""
                    if not text:
                        continue
                    if getattr(message, "type", None) == "ai":
                        final_text = text
                    yield node_name, text
        yield "__final__", final_text
