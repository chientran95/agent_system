# Test Scenarios

A coverage matrix for exercising every capability in the system, grouped by what's actually being tested rather than by file. See [TESTING.md](TESTING.md) for concrete curl/`message/send`/`message/stream` examples of the underlying mechanics.

## 1. Single-agent core behavior (direct A2A, bypass orchestrator)

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

| # | Scenario | Exercises |
|---|---|---|
| 3.1 | Route to each of the 3 sub-agents | `transfer_to_agent` picks the right one for a coding / research / weather request |
| 3.2 | Weather sub-agent pauses via orchestrator | Full stack: ADK sees `input-required` as a paused long-running tool (`longRunningToolIds`) |
| 3.3 | Plain-text follow-up resumes the same paused task | `ResumePendingInputMiddleware` — regression-test it since it's easy to silently break |
| 3.4 | Orchestrator model sanity check | Cheap smoke test: send one trivial routing request and assert the response has a non-empty `functionCall` part, not empty `parts` (see known issues below) |

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

- **Orchestrator-mediated resume of a paused sub-agent task (scenario 1.2/3.3) is broken in ADK's experimental `RemoteA2aAgent`.** Root-caused to `_create_a2a_request_for_user_function_response()` in `google/adk/agents/remote_a2a_agent.py`: on a resume turn, the request produces zero SSE bytes and zero LLM calls — ADK fails to reconstruct "we're mid-delegation to sub-agent X, waiting on its answer" before ever reaching the model. Confirmed *not* caused by: the `ResumePendingInputMiddleware` (verified correctly rewriting the message every time), the remote agent's `streaming` capability flag (tested both ways), or the number of intermediate `working` progress events emitted (tested both ways). Confirmed *not* fixed by upgrading `google-adk` 2.3.0 → 2.5.0 (currently the latest release), despite that release containing several related-looking fixes ("guard against None converter results in RemoteA2aAgent", "a2a: render HITL interrupt when prompt is in a data part"). Direct agent-to-agent resume (bypassing the orchestrator) works perfectly for both code_agent and weather_agent — this is specific to the orchestrator hop.
- **`weather_agent` silently lost its tool definitions to context-window truncation** — fixed. The Open-Meteo MCP server's 17 tool schemas are ~157,816 characters (~40K+ tokens) of JSON; `ChatOllama` wasn't setting `num_ctx`, so Ollama defaulted to a 4096-token context regardless of the model's actual 131,072-token native window, silently truncating the tools out of the request. The model then correctly (from its truncated view) reported having no tools. Fixed by explicitly setting `num_ctx=65536` on `weather_agent.py`'s `ChatOllama` instance.
- **LiteLLM ↔ Ollama tool-calling is intermittently flaky** independent of the above two issues — occasionally returns a response with generated tokens but empty `parts` (no text, no function call), regardless of which local model is used. Seen at multiple call sites (orchestrator's initial routing turn, its post-delegation continuation turn). Root cause not fully isolated; workaround is generally just to retry.
