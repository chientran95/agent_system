# Agent System

A multi-agent Python project with three independent services talking over the A2A protocol:

- **Orchestrator** — Google ADK `LlmAgent`, routing model from a local Ollama (`gpt-oss:20b`), delegates to the two agents below via `RemoteA2aAgent`
- **Coding agent** — Claude Agent SDK, backed by a real Anthropic model, exposed as its own A2A server
- **Content/news agent** — LangChain Deep Agents, backed by a local Ollama model, exposed as its own A2A server
- Reflection loop with two verifier implementations (mechanical for code, rubric-based for content)
- SQLite durable state + local filesystem backend
- OpenTelemetry + Jaeger tracing across all three services (the A2A calls between them are what you'll see as distributed traces)
- LangSmith support for Deep Agents observability

## Structure

- `src/agent_system/orchestrator_server.py`: standalone FastAPI/uvicorn entrypoint for the orchestrator (wraps `google.adk.cli.fast_api.get_fast_api_app`, includes the ADK Dev UI)
- `src/agent_system/orchestrator_agents/orchestrator/agent.py`: the ADK `root_agent` definition and its two `RemoteA2aAgent` sub-agents
- `src/agent_system/code_agent_server.py` / `code_agent.py`: A2A server + Claude Agent SDK logic
- `src/agent_system/content_agent_server.py` / `content_agent.py`: A2A server + Deep Agents logic, SQLite memory, filesystem backend
- `src/agent_system/state.py`: durable SQLite checkpoint storage
- `src/agent_system/settings.py`: environment configuration (ports, URLs, model names)

## Requirements

- Python 3.11+
- `uv` package manager
- `docker` for local Jaeger tracing
- [Ollama](https://ollama.com) running locally with `gpt-oss:20b` pulled (`ollama pull gpt-oss:20b`) — used by the orchestrator and content agent
- An Anthropic API key with credit — used by the coding agent

## Local startup

1. Create a `.env` file from `.env.example` and fill in `CLAUDE_API_KEY`. Defaults are fine for everything else as long as Ollama is running locally.

2. Run Jaeger locally:

```bash
make jaeger
```

3. Start all three services, each in its own terminal (the coding and content agents should be up before the orchestrator, since it calls them over A2A at startup-adjacent request time):

```bash
make run-code-agent      # http://localhost:8001
make run-content-agent   # http://localhost:8002
make run-orchestrator    # http://localhost:8000 (ADK Dev UI included)
```

4. Open the ADK Dev UI at `http://localhost:8000/dev-ui` to interact with the orchestrator and inspect routing/session state, or call the coding/content agents directly via their A2A JSON-RPC endpoints (agent cards are served at `/.well-known/agent-card.json` on each).

5. Open the Jaeger UI at `http://localhost:16686` to see traces spanning the orchestrator's A2A calls into the coding and content agents.

## Verification strategy

- `CodeAgent` verification is purely mechanical: `ruff check`, then `pytest`; no extra LLM calls.
- `ContentAgent` verification is a direct (non-agentic) rubric call to the same local Ollama model, skipping the full drafting agent's tool loop.

## Storage

- `storage/`: local filesystem backend for drafts, notes, and intermediate content
- `state/checkpoint.sqlite`: durable checkpoint data for the content agent
