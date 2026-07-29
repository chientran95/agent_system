# Bug: Orchestrator-mediated resume of a paused sub-agent task silently produces nothing

**Status:** Unresolved — root-caused to `google-adk`'s experimental `RemoteA2aAgent`, no fix or workaround found
**Component:** `google-adk` (third-party), specifically `google/adk/agents/remote_a2a_agent.py`
**Affects:** Resuming a paused (`input-required`) sub-agent task by sending a follow-up message *through the orchestrator*, when that sub-agent is a streaming-capable `RemoteA2aAgent` (i.e. `code_agent`, and now `weather_agent` too)

## Explanation

The system has two independently-verified layers of pause/resume machinery:

1. **The sub-agent's own A2A pause/resume mechanics** (checkpointer, `TaskState.input_required`, `ResilientQueueManager` — see `BUG_A2A_QUEUE_LIFECYCLE.md`). Confirmed solid via direct A2A calls, bypassing the orchestrator entirely, for both `code_agent` and `weather_agent`.
2. **The orchestrator's resume-correlation middleware** (`ResumePendingInputMiddleware`), which detects a pending `mock_function_call_for_required_user_input` in the ADK session and rewraps a plain-text follow-up into the correct `functionResponse` shape ADK expects, so the orchestrator's own LLM doesn't just re-decide `transfer_to_agent` fresh and orphan the paused task.

Layer 2 was originally built and verified against `weather_agent` back when `weather_agent` was non-streaming (`streaming: false`) and used a single blocking call with no intermediate progress events. It was never exercised against `code_agent` (which has always been `streaming: true` and emits multiple intermediate `working` progress events before reaching `input-required`) until this round of testing — and separately, `weather_agent` itself later gained streaming support in this same project, so by the time of this investigation *every* sub-agent was streaming-capable.

When testing `code_agent`'s ask_user pause/resume flow **through the orchestrator** (as opposed to direct A2A), the resume step fails: the orchestrator's own LLM is never even invoked, and the HTTP response comes back as `200 OK` with **zero bytes**, no exception, no error logged anywhere.

Traced this (with temporary debug patches into ADK's own source, all reverted afterward — `git diff` confirms the repo is clean) to `RemoteA2aAgent._create_a2a_request_for_user_function_response()` in `google/adk/agents/remote_a2a_agent.py`. This method is responsible for reconstructing "we are currently mid-delegation to sub-agent X, and this incoming message is the answer it's waiting on" from the ADK session's event history (via `find_matching_function_call(ctx.session.events)`), and using that to build the outgoing A2A resume request with the correct `task_id`/`context_id`. On the failing resume turn, this reconstruction does not happen — no A2A request is built, so nothing is ever sent to the sub-agent, and no LLM call happens either (since there's nothing to route or synthesize a response for).

This code path is explicitly marked experimental by ADK itself:
> `[EXPERIMENTAL] RemoteA2aAgent: ADK Implementation for A2A support ... is in experimental mode and is subject to breaking changes.`

## Symptoms

- In the ADK Dev UI: after answering a clarifying question from `code_agent`, the run "stops" — no further output appears.
- `code_agent`'s own console log shows `Queue is closed. Event will not be dequeued.` — **this turned out to be a red herring / separate, likely-benign symptom**, not the actual cause (see "Tried fixes" below).
- Direct reproduction via `curl` against the orchestrator's `/run_sse`:
  - First call (ambiguous request): `HTTP 200`, correctly reaches `input-required` state.
  - Second call (the resume, same session, plain-text answer): `HTTP 200`, **0 bytes** in the response body, no error in any log.
- Confirmed via debug instrumentation that `LiteLlm.generate_content_async()` — the method that actually calls the LLM — is **never invoked at all** during the failing resume turn.

## Tried fixes / investigation

Extensive, in order:

1. **Suspected recurrence of the `a2a-sdk` queue-lifecycle bug** (`BUG_A2A_QUEUE_LIFECYCLE.md`). Ruled out: a direct `message/stream` pause→resume test against `code_agent`, bypassing the orchestrator entirely, completed flawlessly with zero warnings and the correct final answer. The `ResilientQueueManager` fix is doing its job; this is a different bug.

2. **Confirmed the "Queue is closed" warning is a separate, likely-benign symptom.** It fires on *every* call to `code_agent` made through the orchestrator's `RemoteA2aAgent` client — both the successful first call and the failing resume — but does not correlate with actual data loss (the first call's correct `input-required` response, and the retry's eventually-successful delivery of `code_agent`'s reply into the orchestrator's own context, both still got through despite the warning). Likely a redundant/extra dequeue attempt specific to how `RemoteA2aAgent`'s client consumes a streaming remote agent, distinct from the original queue-closing bug.

3. **Reproduced the exact resume scenario through the real orchestrator**, with the middleware's own dispatch logic wrapped in a try/except and debug-printed. Confirmed the middleware itself works correctly every time — it produces a properly-shaped `functionResponse` targeting `mock_function_call_for_required_user_input` with the right `id`. Ruled out as the cause.

4. **Added temporary debug prints directly into the installed `google-adk` package** (`google/adk/models/lite_llm.py`), at both the streaming and non-streaming branches of `LiteLlm.generate_content_async()`, to capture the raw LiteLLM response object. Discovered the orchestrator's LLM calls are non-streaming (`stream=False`) for this flow, and — critically — **zero debug output at all during the failing resume turn**, confirming the LLM is never called.

5. **Investigated LiteLLM's own `ollama_chat` response transformation** (`litellm/llms/ollama/chat/transformation.py`, both `transform_response` and the streaming `chunk_parser`) for a tool-call-parsing bug that might silently drop content. Found no issue in either — moot anyway once (4) showed the LLM is never reached.

6. **Investigated ADK's own inline-JSON tool-call fallback parser** (`_parse_tool_calls_from_text` / `_split_message_content_and_tool_calls` in `lite_llm.py`), suspecting it might swallow content without successfully extracting a tool call. Also moot given (4).

7. **Tested the hypothesis that the remote agent's `streaming: true` capability flag was the trigger.** Temporarily flipped `code_agent`'s `AgentCard` to `streaming: false`, restarted both `code_agent` and the orchestrator (to force fresh agent-card resolution), and re-ran the identical resume scenario. **Bug persisted identically** (still 0 bytes, no LLM call). Reverted the flag back to `true`.

8. **Tested the hypothesis that multiple intermediate `working` progress events (vs. `weather_agent`'s original single-shot pattern) was the trigger.** Temporarily disabled the progress-update loop in `code_agent_server.py`'s `execute()` so it behaved like a single blocking call with no intermediate events. Re-ran the identical resume scenario. **Bug persisted identically.** Reverted.

9. **Attempted to reproduce with `weather_agent` instead**, to check whether the *originally-verified* agent was now also broken (since it gained streaming support later in this project). Inconclusive: `weather_agent`'s own tool-selection unreliability (separate issue — see below) meant it kept answering directly instead of genuinely pausing, so the resume path was never actually exercised in these attempts.

10. **Traced deeper into ADK's own flow code**: `google/adk/flows/llm_flows/functions.py` (`find_matching_function_call`, `find_event_by_function_call_id`) and `google/adk/a2a/converters/long_running_functions.py` / `to_adk_event.py`. Confirmed the *matching* logic (function-call-id lookup) looks structurally sound; the failure appears to be further upstream, in whether ADK's session/invocation state correctly recalls that the *next* turn should be routed back into the `coding_agent` `RemoteA2aAgent` sub-agent specifically, rather than back to the orchestrator's own top-level agent. Did not fully pin down the exact failing line within the time budgeted for this investigation.

11. **Checked `google-adk`'s changelog between the installed 2.3.0 and the latest 2.5.0** for related fixes. Found several plausibly-relevant entries: *"guard against None converter results in RemoteA2aAgent"* (×2, one for handlers specifically), *"a2a: render HITL interrupt when prompt is in a data part"*, *"avoid yielding a None function-response event in live mode"*.

12. **Upgraded `google-adk` 2.3.0 → 2.5.0**, this time explicitly pinning `a2a-sdk>=0.3.4,<0.4` in `pyproject.toml` alongside the bump (2.5.0 widens its own allowed range to `a2a-sdk<2,>=0.3.4`, so without an explicit pin `uv` would have pulled in the breaking 1.x line again, as happened in an earlier, unrelated upgrade attempt — see `BUG_A2A_QUEUE_LIFECYCLE.md`). Verified `uv sync` resolved to `google-adk==2.5.0` / `a2a-sdk==0.3.26` cleanly, our own code still compiles and imports without error, and all four servers start normally. **Re-ran the identical resume scenario on 2.5.0. Bug persisted identically** (still 0 bytes, no LLM call).

13. **Searched GitHub for an existing upstream issue** matching this symptom (`RemoteA2aAgent` + resume/`input_required`). None found.

14. **Cleaned up all temporary debug patches** — reverted every edit made to the installed `google-adk` package and to our own `orchestrator_resume_middleware.py` / `code_agent_server.py` diagnostic toggles. `git diff` on our own tracked files is clean; the vendored package has zero `DEBUG_` markers remaining.

## Solution

**None found.** This is currently accepted as a known limitation of `google-adk`'s experimental `RemoteA2aAgent` A2A support, not a bug in our own code.

**Decision on the `google-adk` 2.5.0 upgrade:** kept, despite not fixing this specific bug. It's a clean upgrade on its own merits (genuine unrelated security and bug fixes in the 2.4.0/2.5.0 changelog, zero regressions observed against our own code and test suite).

**What still works, and the practical implication:**
- Direct agent-to-agent pause/resume (bypassing the orchestrator) is fully solid for both `code_agent` and `weather_agent` — verified repeatedly, including as part of the mesh bubbling work (see `TEST_SCENARIOS.md`, section 2.3).
- The mesh's own peer-to-peer bubbling mechanism (`PeerInputRequired` → `interrupt()` → resume) is unaffected, since it never routes back through the orchestrator.
- The gap is specifically: **a request that reaches a paused sub-agent's clarifying question *via the orchestrator* cannot currently be answered by sending a plain follow-up message through the orchestrator** — the continuation is silently dropped before the sub-agent (or even the orchestrator's own LLM) is ever called again.

**Possible next steps, not yet attempted:**
- Continue tracing `google/adk/agents/invocation_context.py` and `google/adk/flows/llm_flows/base_llm_flow.py` for exactly where the "currently delegating to sub-agent X" state is supposed to be recovered from session history and is instead being lost.
- File an issue upstream against `google/adk-python` with this write-up as a repro (none currently exists).
- As a product-level workaround, avoid routing agents that need mid-conversation clarification through the orchestrator's long-running-tool pattern — e.g. have sub-agents make a best-effort guess instead of pausing when reached via `transfer_to_agent`, reserving the pause/resume pattern for direct agent-to-agent calls where it's confirmed to work.
