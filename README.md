# Agent System

A multi-agent Python project scaffold with:
- Google ADK orchestrator for A2A routing
- Claude Agent SDK for the code-focused agent
- LangChain Deep Agents for the content/news agent
- Reflection loop with two verifier implementations
- SQLite durable state + local filesystem backend
- OpenTelemetry + Jaeger tracing for the A2A layer
- LangSmith support for Deep Agents observability

## Structure

- `src/agent_system/main.py`: application entrypoint
- `src/agent_system/adk_orchestrator.py`: ADK routing and dispatch
- `src/agent_system/a2a_client.py`: HTTP wrappers with OpenTelemetry tracing
- `src/agent_system/code_agent.py`: code agent and mechanical verifier
- `src/agent_system/content_agent.py`: content agent, SQLite memory, and Filesystem backend
- `src/agent_system/state.py`: durable SQLite checkpoint storage
- `src/agent_system/settings.py`: environment configuration

## Requirements

- Python 3.11+
- `uv` package manager
- `docker` for local Jaeger tracing

## Local startup

1. Create a `.env` file with these values:

```env
LANGCHAIN_API_KEY=your_langchain_key
CLAUDE_API_KEY=your_claude_key
AGENT_STORAGE_DIR=./storage
AGENT_CHECKPOINT_DB=./state/checkpoint.sqlite
CLAUDE_MODEL=claude-haiku-3-5
```

2. Run Jaeger locally:

```bash
docker run -d -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one
```

3. Run the app:

```bash
uv run agent-system
```

4. Open Jaeger UI at `http://localhost:16686`.

5. Use `adk web` to launch the ADK Dev UI and inspect orchestration state.

## Verification strategy

- `CodeAgent` verification is purely mechanical: `tac --noEmit`, then tests; no extra LLM calls.
- `ContentAgent` verification uses a cheap rubric call with `claude-haiku-3-5`.

## Storage

- `storage/`: local filesystem backend for drafts, notes, and intermediate content
- `state/checkpoint.sqlite`: durable checkpoint data for the content agent
