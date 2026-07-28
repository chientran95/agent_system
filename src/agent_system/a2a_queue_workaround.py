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
