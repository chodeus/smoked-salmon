"""In-memory job manager for the web interface.

Two kinds of jobs:
- loop jobs (`create`): coroutines on the server's event loop — used for
  bounded operations like spectral generation or transcoding.
- thread jobs (`create_threaded`): run in a worker thread with their own event
  loop — used for the interactive upload pipeline, whose synchronous prompts
  and filesystem work may block that private loop while the server loop keeps
  serving (and delivers browser answers to pending questions).

Each job exposes status, progress, log tail, pending question and a result;
subscribers (websocket connections) receive every state change as an event.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import itertools
import threading
import time
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncclick as click

from salmon.common.progress import reset_progress_callback, set_progress_callback
from salmon.errors import AbortAndDeleteFolder
from salmon.webui.interaction import WebInteraction, set_interaction

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

_id_counter = itertools.count(1)

MAX_FINISHED_JOBS = 200
SUBSCRIBER_QUEUE_SIZE = 500
MAX_LOG_LINES = 1000
MAX_ACTIVE_THREAD_JOBS = 12  # each threaded job is an OS thread + private event loop
MAX_SUBSCRIBERS = 64  # websocket connections
PROGRESS_BROADCAST_INTERVAL = 0.2  # seconds; job.progress itself always updates


def _make_progress_callback(job: Job, emit: Callable[[dict[str, Any]], None]) -> Callable[[int, int, str], None]:
    """Update job.progress on every call, but throttle the broadcast: huge
    jobs (thousands of files) must not flood every websocket subscriber."""
    last_broadcast = 0.0

    def on_progress(done: int, total: int, desc: str) -> None:
        nonlocal last_broadcast
        job.progress = {"done": done, "total": total, "desc": desc}
        now = time.monotonic()
        if done >= total or now - last_broadcast >= PROGRESS_BROADCAST_INTERVAL:
            last_broadcast = now
            emit({"event": "progress", "job_id": job.id, "progress": job.progress})

    return on_progress


class JobConflictError(Exception):
    """A queued/running job already works on the same path."""


class JobCapacityError(Exception):
    """Too many jobs are already running; the caller should retry later."""


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
        self.question: dict[str, Any] | None = None
        self.log_lines: list[str] = []
        self.interaction: WebInteraction | None = None
        self.thread: threading.Thread | None = None
        self._thread_loop: asyncio.AbstractEventLoop | None = None
        self._thread_task: asyncio.Task | None = None

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
            "question": self.question,
            "log": self.log_lines[-MAX_LOG_LINES:],
            "spectrals": (self.interaction.spectrals or {}).get("files") if self.interaction else None,
        }


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._active_lock_keys: set[str] = set()
        self._server_loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Job creation
    # ------------------------------------------------------------------

    def _register(self, job_type: str, title: str, params: dict[str, Any] | None, lock_key: str | None) -> Job:
        if lock_key is not None:
            if lock_key in self._active_lock_keys:
                raise JobConflictError(f"A job is already running on: {lock_key}")
            self._active_lock_keys.add(lock_key)
        job = Job(job_type, title, params or {})
        job.lock_key = lock_key
        self.jobs[job.id] = job
        self._prune_finished()
        self._server_loop = asyncio.get_running_loop()
        return job

    def create(
        self,
        job_type: str,
        title: str,
        factory: Callable[[Job], Coroutine[Any, Any, Any]],
        params: dict[str, Any] | None = None,
        lock_key: str | None = None,
    ) -> Job:
        """Create a loop job on the server's event loop and start it."""
        job = self._register(job_type, title, params, lock_key)
        job.task = asyncio.create_task(self._run(job, factory))
        job.task.add_done_callback(lambda _t: self._finalize_unstarted(job))
        self._broadcast({"event": "created", "job": job.to_dict()})
        return job

    def create_threaded(
        self,
        job_type: str,
        title: str,
        factory: Callable[[Job], Coroutine[Any, Any, Any]],
        params: dict[str, Any] | None = None,
        lock_key: str | None = None,
    ) -> Job:
        """Create a job that runs in a worker thread with its own event loop."""
        active = sum(1 for j in self.jobs.values() if j.thread is not None and j.status in ("queued", "running"))
        if active >= MAX_ACTIVE_THREAD_JOBS:
            raise JobCapacityError(f"Too many jobs running ({active}); please wait for some to finish.")
        job = self._register(job_type, title, params, lock_key)
        job.interaction = WebInteraction(
            emit=lambda event: self._emit_for(job, event),
            set_question=lambda question: self._set_question(job, question),
        )
        job.thread = threading.Thread(target=self._thread_main, args=(job, factory), daemon=True)
        self._broadcast({"event": "created", "job": job.to_dict()})
        try:
            job.thread.start()
        except BaseException:
            job.status = "error"
            job.error = "Failed to start the worker thread."
            self._finish(job)
            self._broadcast({"event": "finished", "job": job.to_dict()})
            raise
        return job

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _set_status(self, job: Job, status: str, threadsafe: bool = False) -> None:
        job.status = status
        event = {"event": "status", "job_id": job.id, "status": status}
        if threadsafe:
            self._emit(event)
        else:
            self._broadcast(event)

    async def _run(self, job: Job, factory: Callable[[Job], Coroutine[Any, Any, Any]]) -> None:
        self._set_status(job, "running")
        token = set_progress_callback(_make_progress_callback(job, self._broadcast))
        try:
            job.result = await factory(job)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
        except BaseException as e:  # noqa: BLE001 - single funnel for job errors
            self._record_failure(job, e)
        finally:
            reset_progress_callback(token)
            self._finish(job)
            self._broadcast({"event": "finished", "job": job.to_dict()})

    def _thread_main(self, job: Job, factory: Callable[[Job], Coroutine[Any, Any, Any]]) -> None:
        interaction_token = set_interaction(job.interaction)
        progress_token = set_progress_callback(_make_progress_callback(job, self._emit))
        loop = asyncio.new_event_loop()
        job._thread_loop = loop
        try:
            self._set_status(job, "running", threadsafe=True)
            if job.interaction is not None and job.interaction.cancel_requested.is_set():
                raise asyncio.CancelledError()
            task = loop.create_task(factory(job))
            job._thread_task = task
            job.result = loop.run_until_complete(task)
            job.status = "done"
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            job.status = "cancelled"
        except BaseException as e:  # noqa: BLE001 - single funnel for job errors
            if job.interaction is not None and job.interaction.cancel_requested.is_set():
                job.status = "cancelled"
            else:
                self._record_failure(job, e)
        finally:
            reset_progress_callback(progress_token)
            set_interaction(None)
            del interaction_token
            with contextlib.suppress(Exception):
                loop.close()
            job._thread_loop = None
            job.question = None
            self._finish(job)
            self._emit({"event": "finished", "job": job.to_dict()})
            if job.interaction is not None:
                # From here on, leaked to_thread workers must neither emit
                # events nor block on unanswerable questions.
                job.interaction.close()

    def _record_failure(self, job: Job, e: BaseException) -> None:
        job.status = "error"
        leaves = _flatten_exception(e)
        if any(isinstance(leaf, AbortAndDeleteFolder) for leaf in leaves):
            job.error = "Aborted — the album folder was deleted as requested."
        elif any(isinstance(leaf, click.Abort) for leaf in leaves):
            job.error = "Aborted."
        elif len(leaves) > 1 or isinstance(e, BaseExceptionGroup):
            job.error = "; ".join(f"{type(leaf).__name__}: {leaf}" for leaf in leaves)
        else:
            job.error = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    def _finish(self, job: Job) -> None:
        if job.lock_key is not None:
            self._active_lock_keys.discard(job.lock_key)
        job.finished_at = _now()

    def _finalize_unstarted(self, job: Job) -> None:
        if job.finished_at is not None:
            return
        job.status = "cancelled"
        self._finish(job)
        self._broadcast({"event": "finished", "job": job.to_dict()})

    # ------------------------------------------------------------------
    # Interaction plumbing (called from job threads)
    # ------------------------------------------------------------------

    def _emit_for(self, job: Job, event: dict[str, Any]) -> None:
        event = {**event, "job_id": job.id}
        if event.get("event") == "log":
            job.log_lines.append(event["line"])
            if len(job.log_lines) > MAX_LOG_LINES:
                del job.log_lines[: len(job.log_lines) - MAX_LOG_LINES // 2]
        self._emit(event)

    def _set_question(self, job: Job, question: dict[str, Any] | None) -> None:
        job.question = question

    def _emit(self, event: dict[str, Any]) -> None:
        """Thread-safe broadcast: hop onto the server loop when needed."""
        loop = self._server_loop
        if loop is None:
            return
        try:
            if asyncio.get_running_loop() is loop:
                self._broadcast(event)
                return
        except RuntimeError:
            pass
        # RuntimeError: the server loop already closed; the consumer is gone.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._broadcast, event)

    def answer(self, job_id: str, question_id: str, value: Any) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.interaction is None:
            return False
        return job.interaction.answer(question_id, value)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status not in {"queued", "running"}:
            return False
        if job.thread is not None:
            if job.interaction is not None:
                job.interaction.cancel_pending()
            loop, task = job._thread_loop, job._thread_task
            if loop is not None and task is not None:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(task.cancel)
            return True
        if job.task is None or job.task.done():
            return False
        job.task.cancel()
        return True

    def subscribe(self) -> asyncio.Queue | None:
        if len(self._subscribers) >= MAX_SUBSCRIBERS:
            return None
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
