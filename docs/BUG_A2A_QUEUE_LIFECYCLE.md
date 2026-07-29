# Bug: a2a-sdk closes a paused task's EventQueue, breaking resume

**Status:** Fixed (workaround applied in our own code)
**Component:** `a2a-sdk` (third-party, confirmed present in 0.3.26 and 1.1.2)
**Affects:** Any A2A task that pauses in a non-terminal state (`input-required`, `auth-required`) and is later resumed via a follow-up `message/send` or `message/stream` call against the same `taskId`

## Explanation

`a2a-sdk`'s `DefaultRequestHandler` manages one `EventQueue` per task, obtained from a `QueueManager`. The lifecycle has a real bug in how that queue is closed and re-used across turns:

1. **`DefaultRequestHandler._run_event_stream()`** unconditionally calls `await queue.close()` immediately after every single `execute()` call returns — regardless of whether the task ended in a *terminal* state (`completed`, `failed`, `canceled`) or a *non-terminal* one (`input-required`, `auth-required`) that is specifically meant to allow the task to be continued later.

2. **`InMemoryQueueManager`** only removes its own dictionary entry for a `task_id` when the task reaches a genuinely terminal state. For `input-required`, it does *not* clean up — so the manager keeps a reference to a queue object that has already been closed by step 1, and has no way of knowing it's dead.

3. On the **next request** for that same `task_id` (the resume/follow-up message), `InMemoryQueueManager.create_or_tap()` finds the stale entry still in its dictionary and blindly calls `.tap()` on it to create a child queue — but the underlying `EventQueue` was already closed in step 1. Tapping a closed queue does not revive it; any subsequent dequeue attempt on it fails.

Net effect: the *first* pause of a task works fine (you correctly get `input-required` back), but the *second* request — the one meant to resume it — hits a dead queue.

## Symptoms

- Server-side log line: `Queue is closed. Event will not be dequeued.`
- The resume request either hangs, or completes with an empty/near-empty result (e.g. an empty events array) instead of the expected `completed` (or further `input-required`) task state.
- The task's stored state/history is *not* corrupted — the bug is purely in the in-memory event-queue plumbing used to stream/deliver the response, not in the task store itself.

## Tried fixes / investigation

- **Confirmed root cause by reading `a2a-sdk` source directly** rather than guessing: `a2a/server/request_handlers/default_request_handler.py` (`_run_event_stream`, `_cleanup_producer`) and `a2a/server/events/in_memory_queue_manager.py` (`create_or_tap`, `close`).
- **Checked whether upgrading `a2a-sdk` would fix it.** Attempted an upgrade path via `google-adk[a2a]>=2.5.0`, which loosens the allowed `a2a-sdk` range enough to pull in `a2a-sdk` 1.1.2 (a restructured major version — module layout changed, e.g. `a2a.server.apps` moved, which broke our own server code with `ModuleNotFoundError`). Before committing to migrating our code to the new module layout, read 1.1.2's actual source for the equivalent `InMemoryQueueManager`/`_run_event_stream` logic and confirmed **the exact same bug is present there too** — the queue is still unconditionally closed after every `execute()` call regardless of terminal state. Upgrading would have been pure migration cost for zero benefit, so this path was abandoned and the dependency was reverted back to resolve `a2a-sdk` 0.3.26.
- No configuration flag or documented workaround exists in either version to opt out of the unconditional `queue.close()` call.

## Solution

Applied a targeted workaround at the `QueueManager` level rather than patching the closed-source dependency in place. New file `src/agent_system/a2a_queue_workaround.py`:

```python
from a2a.server.events.event_queue import EventQueue
from a2a.server.events.in_memory_queue_manager import InMemoryQueueManager


class ResilientQueueManager(InMemoryQueueManager):
    """Works around a real bug in a2a-sdk (confirmed present in both 0.3.26
    and 1.1.2 - the module moved but the bug didn't): DefaultRequestHandler
    ._run_event_stream() unconditionally closes a task's EventQueue after
    every execute() call, even for non-terminal states like input_required
    that are specifically meant to allow the task to be continued later.
    InMemoryQueueManager.create_or_tap() then blindly taps whatever queue
    is already on file for that task_id on the next request - including a
    dead one - which raises "Queue is closed" instead of resuming.

    This override checks whether the stored queue is already closed before
    deciding to tap it vs. create a fresh one, which is enough to make
    continuing a paused (non-terminal) task actually work. Task history/state
    itself is unaffected - only the in-memory event-queue plumbing changes.

    Remove this once the upstream bug is fixed - watch a2aproject/A2A#1858
    (TaskState.PAUSED proposal) and a2aproject/a2a-python for a fix.
    """

    async def create_or_tap(self, task_id: str) -> EventQueue:
        async with self._lock:
            existing = self._task_queue.get(task_id)
            if existing is not None and existing._is_closed:
                del self._task_queue[task_id]
                existing = None
            if existing is None:
                queue = EventQueue()
                self._task_queue[task_id] = queue
                return queue
            return existing.tap()
```

The override checks the stored queue's own `_is_closed` flag before deciding whether to tap it or create a fresh replacement — instead of blindly trusting that "an entry exists in the manager's dict" means "the queue is alive."

Wired into all three of our own A2A servers by passing it to `DefaultRequestHandler`:

```python
handler = DefaultRequestHandler(
    agent_executor=...,
    task_store=InMemoryTaskStore(),
    queue_manager=ResilientQueueManager(),
)
```

(applied in `code_agent_server.py`, `research_agent_server.py`, `weather_agent_server.py`)

**Verified working:** a direct resume test against `weather_agent` showed the same `task_id` preserved across the pause/resume boundary, with a real (non-empty) `completed` response — where before the fix, the identical scenario returned an empty `[]` response with the "Queue is closed" warning logged.

**Caveat:** this fix covers direct agent-to-agent A2A calls (confirmed solid via multiple direct tests throughout this project, including the mesh peer-calling and code_agent/weather_agent's own ask_user flows). It does **not** cover resume attempts routed *through* the ADK orchestrator's `RemoteA2aAgent` — that turned out to be a separate, still-unresolved bug in `google-adk` itself. See `BUG_ORCHESTRATOR_RESUME.md`.
