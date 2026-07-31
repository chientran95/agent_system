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
- **Recurrence confirmed with `weather_agent`**: after the tool-selection work in `docs/BUG_WEATHER_TOOL_SELECTION.md` got `weather_agent` far enough to genuinely reach `input-required` (asking a clarifying question) via the ADK Dev UI, resuming it hit the identical signature — `input-required` reached correctly, then `Queue is closed. Event will not be dequeued.` logged and no further output. Same bug, same symptom, different sub-agent — consistent with this being a generic `RemoteA2aAgent` issue rather than anything `code_agent`-specific.

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

15. **Attempted a real fix**: wrote `adk_remote_a2a_resume_patch.py`, monkey-patching `RemoteA2aAgent._create_a2a_request_for_user_function_response()` to search backward through `ctx.session.events` for the most recent `user`-authored event (instead of requiring it be the literal last event), on the theory that ADK's own auto-re-issued `transfer_to_agent` call/response pair (generated without an LLM call, to route back into the paused sub-agent) was simply appended *after* the user's answer and burying it. Applied via `_apply_resume_patch()` at the top of `orchestrator_agents/orchestrator/agent.py`, before any `RemoteA2aAgent` is constructed.

16. **The patch did not fix it** — instrumented the patched function directly and found the actual event list for `coding_agent`'s re-entered invocation context is `[user's original first message, transfer_to_agent call, transfer_to_agent response(result=None)]` — **exactly 3 events, identical in shape between the first (pausing) call and the second (resume) call.** Neither the sub-agent's original pause event nor the user's actual answer to it are present in this `ctx.session.events` at all. This falsified the "buried by ordering" theory — it's not that the right event is present-but-not-last, it's that `ctx.session.events` for this re-entered sub-agent invocation genuinely doesn't contain it.

17. **Traced one level up, into `base_llm_flow.py`'s `_run_one_step_async`** (the orchestrator's own top-level flow), and found the actual mechanism (lines ~956-997): it reads `events = invocation_context._get_events(current_invocation=True, current_branch=True)` (a *branch-scoped* view, not the full session), and if `events[-1]` already has an unresolved `function_call` (here: the original `transfer_to_agent` call), it **re-executes that exact same function call directly** via `_postprocess_handle_function_calls_async` — skipping the LLM entirely (matching finding #4: `generate_content_async` is never called). This re-execution constructs a *fresh* branch/invocation context for re-entering `coding_agent`, seeded from the point of the original delegation — not from wherever the sub-agent's own execution left off during its pause, and not carrying the user's new answer into that context either. `RemoteA2aAgent`'s own event-based task/context-ID recovery has nothing to find because the events it needs were never handed to it in the first place - this is a structural mismatch between how ADK's flow reconstructs a branch on re-entry and what `RemoteA2aAgent` assumes will be available, not a narrow off-by-one bug. Reverted the (non-working) patch; `adk_remote_a2a_resume_patch.py` deleted, `agent.py` back to its clean committed state.

## Solution

**None found; a real fix attempt was made and reverted after disproving its premise.** This is a genuine architectural gap between `google-adk`'s branch/invocation-context reconstruction on sub-agent re-entry and what `RemoteA2aAgent`'s continuation logic assumes will be available — not a bug in our own code, and not fixable with a narrow patch to `RemoteA2aAgent` alone (see steps 15-17 above for what was tried and why it didn't work).

**Decision on the `google-adk` 2.5.0 upgrade:** kept, despite not fixing this specific bug. It's a clean upgrade on its own merits (genuine unrelated security and bug fixes in the 2.4.0/2.5.0 changelog, zero regressions observed against our own code and test suite).

**What still works, and the practical implication:**
- Direct agent-to-agent pause/resume (bypassing the orchestrator) is fully solid for both `code_agent` and `weather_agent` — verified repeatedly, including as part of the mesh bubbling work (see `TEST_SCENARIOS.md`, section 2.3).
- The mesh's own peer-to-peer bubbling mechanism (`PeerInputRequired` → `interrupt()` → resume) is unaffected, since it never routes back through the orchestrator.
- The gap is specifically: **a request that reaches a paused sub-agent's clarifying question *via the orchestrator* cannot currently be answered by sending a plain follow-up message through the orchestrator** — the continuation is silently dropped before the sub-agent (or even the orchestrator's own LLM) is ever called again.

**Decision (explicit, not defaulted into):** stop investigating further for now rather than build a custom resume-orchestration layer in our own middleware (the only remaining viable approach - see "Possible next steps" below for what that would entail). Direct agent-to-agent calls remain the workaround for anything needing `input-required`.

**Possible next steps, not yet attempted:**
- **The only approach left that would actually work**, given step 17's finding that the gap is structural: have `ResumePendingInputMiddleware` bypass ADK's broken continuation path entirely rather than trying to make ADK reconstruct it. Concretely: extract the `a2a:task_id`/`a2a:context_id` metadata already present on the sub-agent's original pause event (fetched the same way the middleware already fetches the pending mock-call `id`/`name` today), call the sub-agent's A2A endpoint directly with those IDs (reusing the same direct-resume mechanism already verified working, e.g. via `a2a_peer_client.call_peer_agent`), and hand-construct the ADK-shaped SSE/JSON response ourselves - short-circuiting `call_next(request)` for this one case instead of passing through to ADK's normal handler. This is a real, scoped implementation effort (a small resume-orchestration layer), not a quick patch - explicitly deferred rather than started, pending a decision on whether it's worth the investment versus just using direct agent-to-agent calls for anything that needs `input-required`.
- File an issue upstream against `google/adk-python` with this write-up as a repro (none currently exists).
- As a product-level workaround, avoid routing agents that need mid-conversation clarification through the orchestrator's long-running-tool pattern — e.g. have sub-agents make a best-effort guess instead of pausing when reached via `transfer_to_agent`, reserving the pause/resume pattern for direct agent-to-agent calls where it's confirmed to work.
