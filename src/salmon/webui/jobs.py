"""In-memory job manager for the web interface.

Long-running operations (spectral generation, transcoding, checks, ...) run as
asyncio tasks tracked here. Each job exposes status, progress and a result;
subscribers (websocket connections) receive every state change as an event.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncclick as click

from salmon.common.progress import reset_progress_callback, set_progress_callback

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_id_counter = itertools.count(1)

MAX_FINISHED_JOBS = 200
SUBSCRIBER_QUEUE_SIZE = 500


class JobConflictError(Exception):
    """A queued/running job already works on the same path."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _flatten_exception(e: BaseException) -> list[BaseException]:
    """Flatten (nested) exception groups into their leaf exceptions."""
    if isinstance(e, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in e.exceptions:
            leaves.extend(_flatten_exception(sub))
        return leaves
    return [e]


class Job:
    def __init__(self, job_type: str, title: str, params: dict[str, Any]):
        self.id = f"job-{next(_id_counter)}"
        self.type = job_type
        self.title = title
        self.params = params
        self.status = "queued"
        self.created_at = _now()
        self.finished_at: str | None = None
        self.progress: dict[str, Any] | None = None
        self.result: Any = None
        self.error: str | None = None
        self.task: asyncio.Task | None = None
        self.lock_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._active_lock_keys: set[str] = set()

    def create(
        self,
        job_type: str,
        title: str,
        factory: Callable[[Job], Awaitable[Any]],
        params: dict[str, Any] | None = None,
        lock_key: str | None = None,
    ) -> Job:
        """Create a job and start running it immediately.

        Raises:
            JobConflictError: If lock_key is given and a queued/running job
                holds the same key (same filesystem path).
        """
        if lock_key is not None:
            if lock_key in self._active_lock_keys:
                raise JobConflictError(f"A job is already running on: {lock_key}")
            self._active_lock_keys.add(lock_key)
        job = Job(job_type, title, params or {})
        job.lock_key = lock_key
        self.jobs[job.id] = job
        self._prune_finished()
        job.task = asyncio.create_task(self._run(job, factory))
        # If the task is cancelled before its coroutine ever runs, _run's
        # finally never executes — finalize from the done callback instead.
        job.task.add_done_callback(lambda _t: self._finalize_unstarted(job))
        self._broadcast({"event": "created", "job": job.to_dict()})
        return job

    def _finalize_unstarted(self, job: Job) -> None:
        if job.finished_at is not None:
            return
        job.status = "cancelled"
        if job.lock_key is not None:
            self._active_lock_keys.discard(job.lock_key)
        job.finished_at = _now()
        self._broadcast({"event": "finished", "job": job.to_dict()})

    async def _run(self, job: Job, factory: Callable[[Job], Awaitable[Any]]) -> None:
        def on_progress(done: int, total: int, desc: str) -> None:
            job.progress = {"done": done, "total": total, "desc": desc}
            self._broadcast({"event": "progress", "job_id": job.id, "progress": job.progress})

        job.status = "running"
        self._broadcast({"event": "status", "job_id": job.id, "status": job.status})
        token = set_progress_callback(on_progress)
        try:
            job.result = await factory(job)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
        except BaseExceptionGroup as e:
            leaves = _flatten_exception(e)
            job.status = "error"
            if any(isinstance(leaf, click.Abort) for leaf in leaves):
                job.error = "Operation aborted by the underlying tool."
            else:
                job.error = "; ".join(f"{type(leaf).__name__}: {leaf}" for leaf in leaves)
            traceback.print_exc()
        except click.Abort:
            job.status = "error"
            job.error = "Operation aborted by the underlying tool."
        except Exception as e:
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            reset_progress_callback(token)
            if job.lock_key is not None:
                self._active_lock_keys.discard(job.lock_key)
            job.finished_at = _now()
            self._broadcast({"event": "finished", "job": job.to_dict()})

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.task is None or job.task.done():
            return False
        job.task.cancel()
        return True

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _broadcast(self, event: dict[str, Any]) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest event; a stalled consumer resyncs via GET /jobs.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    def _prune_finished(self) -> None:
        finished = [j for j in self.jobs.values() if j.status in {"done", "error", "cancelled"}]
        if len(finished) <= MAX_FINISHED_JOBS:
            return
        for job in finished[: len(finished) - MAX_FINISHED_JOBS]:
            del self.jobs[job.id]


manager = JobManager()
