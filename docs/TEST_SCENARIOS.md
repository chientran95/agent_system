# Test Scenarios

A coverage matrix for exercising every capability in the system, grouped by what's actually being tested rather than by file. See [TESTING.md](TESTING.md) for concrete curl/`message/send`/`message/stream` examples of the underlying mechanics.

## 1. (DONE) Single-agent core behavior (direct A2A, bypass orchestrator)

| # | Scenario | Target | Input | Expect |
|---|---|---|---|---|
| 1.1 | Clean code generation | code_agent | "Write a Python function that reverses a string." | Completed, code in artifact, no tool calls |
| 1.2 | Ambiguous code request → clarify → resume | code_agent | "Write a validation function." → answer | `input-required` with a real question, then `completed` with matching code, same session resumed |
| 1.3 | Research happy path | research_agent | A topic needing only web search, no code | `completed`, non-empty `blog_post` artifact |
| 1.4 | Well-specified weather query | weather_agent | "Current weather in Tokyo, Japan?" | Picks a sensible tool (`weather_forecast` or a national model), calls `geocoding` first if given a place name |
| 1.5 | Underspecified weather query → clarify → resume | weather_agent | "What's the weather like today?" → "Berlin, Germany" | `input-required` then `completed`, same `taskId`/thread resumed |
| 1.6 | Streaming variant of 1.1/1.4 | code_agent, weather_agent | Same prompts via `message/stream` | Multiple `working` progress events (not just one) before `completed` — for weather_agent specifically, confirm you see both an `[agent]` and a `[tools]` chunk |

## 2. Mesh mechanics (peer-to-peer, bubbling, registry, depth limit)

| # | Scenario | Exercises | How |
|---|---|---|---|
| 2.1 | code_agent → research_agent delegation | `call_research_agent` tool, `call_peer_agent_by_name` | Ask code_agent something needing current library/API info it shouldn't know cold |
| 2.2 | research_agent → code_agent delegation | `call_code_agent` tool | Ask research_agent to research + hand off an implementation step (needs a very directive prompt — the deep agent frequently skips this on its own, that's expected model unreliability, not a bug) |
| 2.3 | Bubbling: code_agent asks a clarifying question mid-delegation from research_agent | `PeerInputRequired` → `interrupt()` → resume | Best verified via a direct tool-level test (call `call_code_agent` inside a minimal LangGraph graph) rather than through the flaky deep-agent path, since it isolates the mechanism from the LLM's tool-choice judgment |
| 2.4 | Depth limit enforcement | `MAX_MESH_CALL_DEPTH` | Not reachable through normal prompts (no real cycle exists) — test it directly in Python: call `call_peer_agent(url, text, call_depth=10)` and assert `MeshDepthExceeded` is raised without any HTTP call being made |
| 2.5 | Registry resolves before Direct Config | `lookup_agent_url` | `agent_registry.json` has correct URLs — assert `lookup_agent_url("coding_agent") == CODE_AGENT_URL` |
| 2.6 | Fallback when agent isn't registered | `call_peer_agent_by_name` | Temporarily remove an entry from `agent_registry.json`, confirm the mesh call still succeeds via the `settings.py` fallback, then restore the file |
| 2.7 | Registry override actually changes routing | Registry precedence | Point `agent_registry.json`'s `coding_agent` URL at a wrong port, confirm `call_peer_agent_by_name` fails/misroutes there rather than to the real one — proves the registry, not the fallback, was actually used |

## 3. Orchestrator routing + resume-correlation

Two interchangeable implementations, switched via `ORCHESTRATOR_BACKEND` in `.env` — only one runs at a time, same port. See [curl_commands.md](curl_commands.md) scenario 3 for both backends' request shapes.

| # | Scenario | Exercises |
|---|---|---|
| 3.1 | Route to each of the 3 sub-agents | ADK backend: `transfer_to_agent` picks the right one. LangGraph backend: `plan_node`'s structured-output `Plan` degrades to a single wave/single step for a single-agent request - same coverage, different mechanism |
| 3.2 | Weather sub-agent pauses via orchestrator | ADK backend: full stack, ADK sees `input-required` as a paused long-running tool (`longRunningToolIds`). LangGraph backend: the paused step's question surfaces as a normal A2A `input-required`, no ADK-specific representation to check |
| 3.3 | Plain-text follow-up resumes the same paused task | ADK backend: `ResumePendingInputMiddleware` correlates the follow-up correctly, but the deeper resume through `RemoteA2aAgent` is still broken (see known issues below). LangGraph backend: resumes correctly end-to-end via `interrupt()`/checkpointer - sidesteps the ADK bug entirely rather than fixing it |
| 3.4 | Orchestrator model sanity check | ADK backend: cheap smoke test, assert the response has a non-empty `functionCall` part, not empty `parts` (see known issues below). LangGraph backend: agent-card fetch is sufficient - `plan_node` failures surface as real errors, not silently-empty responses |
| 3.5 | Multi-step plan: parallel wave + sequential dependency | LangGraph backend only. A request needing 2+ independent agents plus one agent that depends on another's output - confirm the plan groups independent steps into the same wave (dispatched via `Send`, run concurrently) and puts the dependent step in a later wave, then confirm `summarize_node` combines all results coherently |

## 4. Infrastructure / failure-mode tests

| # | Scenario | Exercises |
|---|---|---|
| 4.1 | Resume after "Queue is closed" scenario | `ResilientQueueManager` — pause a task, wait, resume; confirm no `asyncio.QueueEmpty` |
| 4.2 | Two concurrent sessions to the same agent | Confirms task/thread isolation (no cross-talk between `taskId`s) |
| 4.3 | Empty/malformed peer response | Mock or stub a peer returning a JSON-RPC `error` — confirm `call_peer_agent` raises cleanly rather than hanging |
| 4.4 | code_agent's `mechanical_verify` | `ruff check` + `pytest` gate still fails closed on bad code |

## 5. Observability spot-checks

| # | Scenario | Where to look |
|---|---|---|
| 5.1 | One trace spans orchestrator → sub-agent | Jaeger, search by `orchestrator` service, confirm child spans from e.g. `research_agent_a2a` |
| 5.2 | Claude Code's own spans appear | Jaeger, `claude_code_cli` service, one trace per code_agent request |
| 5.3 | Langfuse prompt/completion detail | Langfuse dashboard for research_agent, content_writer, weather_agent runs |
| 5.4 | Orchestrator's dual OTLP export | Langfuse shows orchestrator routing spans (structure/tokens, not raw text — known limitation) |

## 6. Storage

| # | Scenario | Verify |
|---|---|---|
| 6.1 | Draft persistence | `storage/draft-*.md` created after a `content_writer` run |
| 6.2 | Checkpoint DB | `state/checkpoint.sqlite` gets written to after a content-writing run |

---

## Known issues found via this walkthrough

- **Orchestrator-mediated resume of a paused sub-agent task (scenario 1.2/3.3) is broken on the ADK backend** (`ORCHESTRATOR_BACKEND=adk`), in ADK's experimental `RemoteA2aAgent`. Direct agent-to-agent resume (bypassing the orchestrator) works fine for both code_agent and weather_agent — this is specific to the orchestrator hop. Not fixed by upgrading `google-adk` 2.3.0 → 2.5.0, and no narrow patch is possible (root cause is architectural - see the write-up). **Sidestepped, not fixed**, by the LangGraph orchestrator backend (`ORCHESTRATOR_BACKEND=langgraph`), which reuses the mesh's own `interrupt()`/checkpointer pattern instead of depending on ADK's broken branch reconstruction - resume works correctly there. Full write-up: [BUG_ORCHESTRATOR_RESUME.md](BUG_ORCHESTRATOR_RESUME.md).
- **`weather_agent` fails to select a tool from its 17-tool MCP toolset** (scenarios 1.4/1.5) — fixed, but not via the originally-suspected cause. A context-window truncation bug is fixed (`ChatOllama` now sets `num_ctx=65536`). The deeper failure turned out not to be tool-schema volume (disproven: even 3 tools / a raw Ollama call outside any Python framework failed identically) but specific to `mistral-small3.2:24b`'s tool-calling for this prompt shape. `weather_agent` has since moved off Ollama entirely onto NVIDIA's hosted API (`ChatNVIDIA`, currently `stepfun-ai/step-3.7-flash`), still combined with the two-pass tool selection (`select_tools` node picks ~3 tools by name+description only, then only those get bound with full schemas). Full write-up: [BUG_WEATHER_TOOL_SELECTION.md](BUG_WEATHER_TOOL_SELECTION.md).
- **`weather_agent`'s MCP tool calls intermittently failed with a blank error message** — fixed. `get_tools()` spun up a brand-new `open-meteo-mcp-server` subprocess for *every* tool call (documented behavior of `langchain_mcp_adapters`, easy to miss), and the freshly-spawned process's first real API call would sometimes race ahead of its own network readiness and fail with no useful error text. Fixed by switching to a persistent MCP session opened once at startup (`client.session(...)` + `AsyncExitStack`) instead of a fresh one per call. Full write-up: [BUG_MCP_SESSION_PER_CALL.md](BUG_MCP_SESSION_PER_CALL.md).
- **LiteLLM ↔ Ollama tool-calling is intermittently flaky** independent of the above issues — occasionally returns a response with generated tokens but empty `parts` (no text, no function call), regardless of which local model is used. Seen at multiple call sites (orchestrator's initial routing turn, its post-delegation continuation turn). Root cause not fully isolated; workaround is generally just to retry.
- **`research_agent`'s `mistral-small3.2:24b` (via Ollama) sometimes invented tool-call kwargs instead of using the declared schema** (scenario 2.2) — e.g. calling `call_code_agent` with `{'description': ...}` instead of its actual `request` argument, or deepagents' built-in `task` tool with `{'task_description': ...}` instead of `description`. Seen against two independently-schema'd tools (one ours, one deepagents' own), so it wasn't a docstring-wording issue on our side. A `ChatNVIDIA` migration (mirroring `weather_agent`'s fix) was tried and reverted — it surfaced a second, NVIDIA-endpoint-specific issue (`400: Assistant message must have either content or tool_calls, but not both`, since the model narrates before calling a tool and that message re-enters history verbatim). **Fixed** by moving `research_agent` to Anthropic's hosted API directly (`ChatAnthropic`, `claude-haiku-4-5`) instead — verified live doing the full intended web_search → content_writer procedure correctly, no invented kwargs, no over-eager clarifying questions on well-specified topics. No NVIDIA-style content/tool_calls conflict either (Anthropic's native API has no such restriction).
