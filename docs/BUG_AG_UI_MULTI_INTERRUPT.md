# Bug: multiple simultaneous interrupts in a wave weren't handled correctly

**Status:** Fixed - one bug in our own code, one bug in a third-party dependency (worked around by not depending on it)
**Components:** `orchestrator_agent.py` (ours), `ag-ui-langgraph` 0.0.42 (third-party, not used - see "Solution")
**Affects:** A wave with 2+ steps that all pause (`interrupt()`) at the same time - e.g. `weather_agent` needing a location AND `code_agent` needing a language, dispatched in the same parallel wave

## Explanation

Building an AG-UI frontend for the wave-based orchestrator required first confirming multi-simultaneous-interrupt handling actually works end-to-end, not just in the single-interrupt case every other pause/resume flow in this project has exercised so far.

Two independent bugs were found, in two different places:

### Bug 1 (third-party): `ag-ui-langgraph` only reads `tasks[0].interrupts`

Verified via a standalone spike: built a FastAPI server using `ag-ui-langgraph==0.0.42`'s `add_langgraph_fastapi_endpoint`/`LangGraphAgent`, wrapping a graph shaped exactly like `orchestrator_agent.py` (`Send()`-based parallel fan-out + `interrupt()`). Live HTTP requests confirmed:

- When two branches paused simultaneously, only **one** `on_interrupt` event reached the client - the second was silently dropped. Root cause, found in the package's own source: its live-stream interrupt collection reads `tasks[0].interrupts` (first task only), not all tasks. A *different* code path in the same file (`_collect_interrupts`) does iterate all tasks correctly, with a comment referencing a prior fix for exactly this class of bug (`#1409`) - the fix exists in their codebase, just not applied consistently.
- Resuming with a plain scalar value (the only resume mechanism the package's wired-up code path supports) while two interrupts were pending crashed with an uncaught `RuntimeError: When there are multiple pending interrupts, you must specify the interrupt id when resuming` - the same error LangGraph itself raises, confirmed via an earlier raw-LangGraph spike. Because this happens *after* the HTTP response already started streaming, it can't degrade into a clean error event - it just drops the connection (`httpx.RemoteProtocolError: peer closed connection without sending complete message body`).

The AG-UI core protocol schema (`ag_ui.core.types`, separate package `ag-ui-protocol`) already has the right shape for this - `RunAgentInput.resume: list[ResumeEntry]` (each with an `interrupt_id`) and `RunFinishedEvent.outcome=RunFinishedInterruptOutcome` (`interrupts: list[Interrupt]`) - but `ag-ui-langgraph`'s adapter never reads or writes either field. Cross-checked against the wider ecosystem: [AG-UI discussion #827](https://github.com/ag-ui-protocol/ag-ui/discussions/827) confirms the protocol maintainers know multi-interrupt support is incomplete and were still drafting it as of the discussion's dates - not shipped anywhere yet.

### Bug 2 (ours): `_astream_graph` overwrote instead of accumulating interrupts

While building the fix for Bug 1, discovered a real bug in our own code that Bug 1's investigation had been masking. `_astream_graph` (in `orchestrator_agent.py`) did:

```python
if node_name == "__interrupt__":
    interrupted = list(node_output) if node_output else []
```

This assumes all simultaneous interrupts arrive together in one `__interrupt__` chunk - true for `graph.ainvoke()`'s final combined state (confirmed via an earlier spike), but **not** true for `graph.astream(..., stream_mode="updates")` (what `_astream_graph` actually uses): confirmed via a standalone repro that streaming mode emits a **separate** `__interrupt__` chunk per interrupted branch. `interrupted = list(node_output)` silently overwrote on each chunk, so only the *last* branch's interrupt survived by the end of the loop - every earlier one was dropped, project-wide, not just when going through the buggy third-party adapter.

## Solution

**Bug 2 (ours):** `interrupted.extend(node_output or [])` instead of overwriting - accumulates across however many `__interrupt__` chunks arrive in a single streaming pass.

**Bug 1 (third-party):** rather than patch `ag-ui-langgraph`'s internals (the buggy line lives inside a large private method, not a small isolable one - patching it would mean copying a chunk of someone else's fast-moving pre-1.0 internals into our own code), built a hand-rolled AG-UI translation layer instead - `orchestrator_agui_server.py`. It depends only on the low-level, non-buggy parts of the AG-UI SDK (`ag_ui.core` event/type definitions, `ag_ui.encoder` for SSE), and wraps `OrchestratorAgent`'s own stream directly - the same shape `orchestrator_langgraph_server.py` already uses to translate that stream into A2A. Concretely:

- `OrchestratorAgent._pending_interrupts: dict[str, list[Interrupt]]` (was `_pending_interrupt_ids: dict[str, str]`) - tracks *all* pending interrupts per thread, not just one.
- `OrchestratorAgent.get_pending_interrupts(thread_id)` - new accessor, the full list.
- `OrchestratorAgent.aresume_route()` now accepts either a plain string (A2A's contract - resolves the first pending interrupt only, since A2A callers never send an interrupt id) or a `dict[str, str]` of `{interrupt_id: answer}` (AG-UI's contract - resolves one or more specific interrupts at once). Partial resume (some pending interrupts answered, others not) was verified working correctly in an earlier spike - unanswered ones correctly re-surface on the next call rather than being lost or forcing an all-or-nothing resume.
- `orchestrator_agui_server.py` reports every pending interrupt via `RunFinishedEvent.outcome=RunFinishedInterruptOutcome(interrupts=[...])` and accepts resume requests via `RunAgentInput.resume: list[ResumeEntry]` - using the AG-UI core protocol's own purpose-built shape for this, not an ad-hoc convention.

**Verified end-to-end**, not just at the unit level: a live HTTP request against the real `OrchestratorAgent` with a mocked mesh (two peer agents, each needing one round of clarification) correctly surfaced both interrupts in one `RunFinishedInterruptOutcome`, and resuming both at once via a proper `resume` array completed the run successfully with no error and no dropped connection.

**A2A is unaffected** - `orchestrator_langgraph_server.py` (and every other agent's server) still passes plain strings to `aresume_route`/equivalent, hitting the same single-interrupt-at-a-time code path as before. No behavior change there.
