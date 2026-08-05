# Agent System

A multi-agent Python project with three independent services talking over the A2A protocol, plus an orchestrator:

- **Orchestrator** — two interchangeable implementations, switched via `ORCHESTRATOR_BACKEND` in `.env` (only one runs at a time, same port):
  - `adk` (default) — Google ADK `LlmAgent`, routing model from a local Ollama (`gpt-oss:20b`), delegates to the three agents below via `RemoteA2aAgent`. Has a known bug where resuming a paused sub-agent task *through* the orchestrator silently fails — see [docs/BUG_ORCHESTRATOR_RESUME.md](docs/BUG_ORCHESTRATOR_RESUME.md).
  - `langgraph` — hand-built LangGraph graph (`orchestrator_agent.py`), backed by Claude Haiku 4.5. Plans a request into an ordered list of *waves* of steps (steps within a wave run in parallel via `Send`, waves run in sequence for steps that depend on an earlier step's output), dispatches each step over the same mesh primitives the agents use to call each other, then summarizes the combined results. Speaks plain A2A JSON-RPC (same shape as the three agents below) rather than ADK's session-REST API, and sidesteps the ADK resume bug entirely rather than fixing it.
- **Coding agent** — Claude Agent SDK, backed by a real Anthropic model, exposed as its own A2A server
- **Research agent** — LangChain Deep Agents, backed by Anthropic's hosted API (`claude-haiku-4-5`) directly. Researches a topic via a free DuckDuckGo web search tool, then delegates to an internal `content_writer` subagent (a plain LangGraph graph, not deepagents) to draft and verify a publish-ready blog post. The content-writing piece is not independently exposed - it's only reachable by going through the research agent. (Previously ran on a local Ollama model; moved to Anthropic after `mistral-small3.2:24b` demonstrably invented tool-call kwargs against correctly-specified schemas — see [docs/TEST_SCENARIOS.md](docs/TEST_SCENARIOS.md)'s known issues.)
- **Weather agent** — hand-built LangGraph graph (agent/tools loop, no deepagents), backed by NVIDIA's hosted API (`ChatNVIDIA`), connected to the [`open-meteo-mcp`](https://github.com/cmer81/open-meteo-mcp) MCP server's 17 weather/climate tools. Demonstrates two things: tool selection among many overlapping options (multiple national-weather-model tools alongside the generic ones), and a human-in-the-loop pattern - a custom `ask_user` tool calls LangGraph's `interrupt()` when a request is missing information, which the A2A server surfaces as a paused (`input-required`) task that a follow-up message resumes.
- **Agent mesh** — any agent (including the LangGraph orchestrator) can call any other agent peer-to-peer over A2A, with call-depth limiting and pause/resume bubbling across hops - see "Agent mesh" below
- Reflection loop with two verifier implementations (mechanical for code, rubric-based for content)
- SQLite durable state + local filesystem backend
- OpenTelemetry + Jaeger tracing across all services (the A2A calls between them are what you'll see as distributed traces)
- Optional Langfuse tracing for LLM-semantic detail (prompts/completions/cost) on the LangChain-based research/content/weather/orchestrator paths

## Structure

- `src/agent_system/orchestrator_main.py`: entry point for `uv run orchestrator` - picks which orchestrator implementation actually starts, based on `ORCHESTRATOR_BACKEND`
- `src/agent_system/orchestrator_server.py`: ADK backend - standalone FastAPI/uvicorn entrypoint (wraps `google.adk.cli.fast_api.get_fast_api_app`, includes the ADK Dev UI)
- `src/agent_system/orchestrator_agents/orchestrator/agent.py`: ADK backend - the ADK `root_agent` definition and its three `RemoteA2aAgent` sub-agents
- `src/agent_system/orchestrator_agent.py`: LangGraph backend - the plan/dispatch/summarize graph
- `src/agent_system/orchestrator_langgraph_server.py`: LangGraph backend - A2A server (same shape as the three agent servers below)
- `src/agent_system/code_agent_server.py` / `code_agent.py`: A2A server + Claude Agent SDK logic
- `src/agent_system/research_agent_server.py` / `research_agent.py`: A2A server + deepagents logic (web search tool, delegates to the content subagent)
- `src/agent_system/content_agent.py`: LangGraph graph (draft + persist/verify nodes) used only as `research_agent`'s `content_writer` subagent - not run as its own server
- `src/agent_system/weather_agent_server.py` / `weather_agent.py`: A2A server + hand-built LangGraph agent, MCP tool loading, interrupt/resume handling
- `src/agent_system/a2a_peer_client.py`: mesh peer-calling primitives (`call_peer_agent_by_name`, `PeerInputRequired`, call-depth limiting) - shared by the agents' own delegation tools and the LangGraph orchestrator's `dispatch_step`
- `src/agent_system/a2a_tracing.py` / `langfuse_tracing.py`: OpenTelemetry/Jaeger and optional Langfuse wiring
- `src/agent_system/state.py`: durable SQLite checkpoint storage
- `src/agent_system/settings.py`: environment configuration (ports, URLs, model names)

## Requirements

- Python 3.11+
- `uv` package manager
- `docker` for local Jaeger tracing
- An Anthropic API key with credit (`CLAUDE_API_KEY`) — used by the coding agent, the research agent, and (if `ORCHESTRATOR_BACKEND=langgraph`) the orchestrator
- An NVIDIA API key (`NVIDIA_API_KEY`, free tier) — used by the weather agent
- [Ollama](https://ollama.com) running locally with `gpt-oss:20b` pulled (`ollama pull gpt-oss:20b`) — only needed for `content_agent` and, if `ORCHESTRATOR_BACKEND=adk` (the default), the orchestrator
- Node.js 22+ (`node`/`npx` on `PATH`) — the weather agent spawns `open-meteo-mcp-server` via `npx` on demand, no separate process to run yourself
- No API key needed for web search: it uses DuckDuckGo via `ddgs`
- Optional: a [Langfuse](https://cloud.langfuse.com) project (public/secret key) for LLM-semantic tracing

## Local startup

1. Create a `.env` file from `.env.example` and fill in `CLAUDE_API_KEY` and `NVIDIA_API_KEY`. Defaults are fine for everything else as long as Ollama is running locally for `content_agent` (and the orchestrator, if left on the default `ORCHESTRATOR_BACKEND=adk`). `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are optional - leave blank to skip Langfuse.

2. Run Jaeger locally:

```bash
make jaeger
```

3. Start all four services, each in its own terminal (the coding, research, and weather agents should be up before the orchestrator, since it calls them over A2A at startup-adjacent request time):

```bash
make run-code-agent      # http://localhost:8001
make run-research-agent  # http://localhost:8002
make run-weather-agent   # http://localhost:8003
make run-orchestrator    # http://localhost:8000 - ADK backend (default) or LangGraph backend, per ORCHESTRATOR_BACKEND
```

4. With the default `ORCHESTRATOR_BACKEND=adk`, open the ADK Dev UI at `http://localhost:8000/dev-ui` to interact with the orchestrator and inspect routing/session state. With `ORCHESTRATOR_BACKEND=langgraph`, there's no Dev UI - call it the same way as the three agents, over A2A JSON-RPC. Either way, you can call any of the three agents (or the LangGraph orchestrator) directly via their A2A JSON-RPC endpoints (agent cards are served at `/.well-known/agent-card.json` on each) - see [docs/curl_commands.md](docs/curl_commands.md).

5. Open the Jaeger UI at `http://localhost:16686` to see traces spanning the orchestrator's A2A calls into the other agents. If Langfuse keys are set, open your Langfuse project dashboard to see prompt/completion-level detail for the research, content, weather, and (LangGraph backend only) orchestrator agents.

## Verification strategy

- `CodeAgent` verification is purely mechanical: `ruff check`, then `pytest`; no extra LLM calls.
- The content-writing subagent's verification is a direct (non-agentic) rubric call to the same local Ollama model, skipping the full drafting agent's tool loop - it runs automatically as part of `content_writer`'s graph, not as a separate step you call.

## Human-in-the-loop: weather agent (and research agent)

The weather agent can pause mid-request and ask a clarifying question, rather than guessing at missing information (e.g. no location given). The primary mechanism is a required-or-null `clarifying_question` field on `weather_agent`'s tool-selection structured-output call (`select_tools_node`) - more reliable than asking the model to separately decide whether to invoke a side-channel tool, which was the original design and proved unreliable (see [docs/BUG_WEATHER_TOOL_SELECTION.md](docs/BUG_WEATHER_TOOL_SELECTION.md)). A plain `ask_user` tool still exists as a fallback inside the main agent turn. `research_agent` uses the identical pattern via `ClarifyTopicMiddleware`. Mechanically, once a pause is triggered:

1. `interrupt(question)` (LangGraph) pauses the graph.
2. The graph is compiled with a checkpointer (`InMemorySaver`), so the paused state persists.
3. The A2A server detects the interrupt and publishes the task as `TaskState.input_required` with the question, instead of completing it.
4. A follow-up A2A message referencing the same `taskId` resumes the same checkpointed graph state via `Command(resume=answer)`.

See [TESTING.md](TESTING.md) for a full worked example. The pause/resume mechanism itself is solid across every agent that uses it (including the LangGraph orchestrator's `dispatch_step`, and its own multi-simultaneous-interrupt case under parallel wave dispatch). Whether the model *acts correctly* once resumed is a separate, model-dependent concern.

## Agent mesh

Beyond the orchestrator's hub-and-spoke routing, agents can call each other directly (peer-to-peer) over A2A - e.g. `research_agent` can delegate a code-writing step straight to `coding_agent` mid-task, without going back through the orchestrator. The LangGraph orchestrator backend is itself built on these same primitives (`dispatch_step` calls `call_peer_agent_by_name` per planned step) - it's a mesh participant, not a separate routing mechanism, unlike the ADK backend's `RemoteA2aAgent`. This is implemented in `a2a_peer_client.py`:

- **`call_peer_agent(url, text, call_depth)`** sends a single A2A `message/send` to another agent and returns its final text, or raises `PeerInputRequired` if the callee paused (`input-required`) instead of completing.
- **Call-depth limiting**: every mesh call increments a `mesh_call_depth` counter carried in the A2A message metadata; calls that would exceed `MAX_MESH_CALL_DEPTH` (10) are refused with `MeshDepthExceeded`, guarding against call cycles between agents.
- **Bubbling paused state across hops**: if a callee pauses (e.g. `coding_agent` needs a clarifying answer while `research_agent` delegated to it), the caller doesn't swallow that - `research_agent`'s side calls LangGraph's `interrupt()` so its *own* task pauses too, propagating the `input-required` state up to whoever called it, however many hops away. Answering the outermost paused task threads the answer back down and resumes the original call.

### Agent discovery: Curated Registry + Direct Configuration

Both discovery strategies are wired up side by side, deliberately, so each can be exercised independently:

- **Curated Registry** (`agent_registry.py` / `agent_registry.json`): a maintainer-curated JSON file mapping each agent's `AgentCard` name to its base URL and description. `lookup_agent_url(name)` reads it; returns `None` if the agent isn't listed.
- **Direct Configuration**: the existing `*_AGENT_URL` settings in `settings.py`, sourced from env vars.

Every discovery site - the mesh's `call_peer_agent_by_name(name, fallback_url, ...)` (used directly by the LangGraph orchestrator too) and the ADK orchestrator's `RemoteA2aAgent` construction in `orchestrator_agents/orchestrator/agent.py` - tries the registry first and falls back to Direct Configuration if the agent isn't registered. Editing `agent_registry.json` changes routing with no code changes; deleting an agent's entry from it (or the whole file) falls back to Direct Configuration with no errors.

## Storage

- `storage/`: local filesystem backend for drafts, notes, and intermediate content
- `state/checkpoint.sqlite`: durable checkpoint data for the content-writing subagent
