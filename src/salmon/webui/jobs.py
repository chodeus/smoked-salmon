"""In-memory job manager for the web interface.

Each job runs in a worker thread with its own event loop: its synchronous prompts and filesystem work may block that
private loop while the server loop keeps serving (and delivers browser answers to pending questions). At most
MAX_JOBS run at once; the others wait their turn in the order they came, and a job waits while another works on the
same album folder. What a job prints goes to its log (see output.py).

Each job exposes status, progress, log tail, pending question and a result;
subscribers (websocket connections) receive every state change as an event.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import asyncclick as click

from salmon.common.progress import reset_progress_callback, set_progress_callback
from salmon.common.redaction import redact_url
from salmon.errors import AbortAndDeleteFolder, NoAnswerError, UnknownOutcomeError
from salmon.trackers import account as accounts
from salmon.uploader.record import recording
from salmon.webui import egress, output
from salmon.webui.interaction import WebInteraction, no_answer, set_interaction

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

_id_counter = itertools.count(1)

FINISHED = frozenset({"done", "failed", "cancelled", "unknown_outcome"})
UNKNOWN_OUTCOME = "The request may have reached the tracker: check the site before trying again."
MAX_FINISHED_JOBS = 200
SUBSCRIBER_QUEUE_SIZE = 500
MAX_LOG_LINES = 1000
# Jobs running at once, each an OS thread with its own event loop; the others wait their turn, at most MAX_QUEUED_JOBS.
MAX_JOBS = 2
MAX_QUEUED_JOBS = 100
# How long a job's end waits for the worker threads it left, as asyncio.run does.
WORKER_JOIN_TIMEOUT = 300
# How long stopping salmon web waits for its running jobs.
STOP_TIMEOUT = 30
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
    """A queued/running job already holds the same lock."""


class JobCapacityError(Exception):
    """Too many jobs are already waiting; the caller should retry later."""


class FolderChangedError(Exception):
    """A job's album folder is no longer the one its request named, where it was."""


def _unknown(noted: list[UnknownOutcomeError], raised: BaseException | None = None) -> UnknownOutcomeError:
    """One error for every request the run could not tell about: those it noted, then the one it stopped on."""
    stopped_on = None if raised is None else _unknown_outcome(raised)
    if stopped_on is not None and all(stopped_on is not each for each in noted):
        noted = [*noted, stopped_on]
    return UnknownOutcomeError("; ".join(str(error) for error in noted))


async def _recorded(coro: Coroutine[Any, Any, Any]) -> Any:
    """The job's run, ending as an unknown outcome when it went on past a request whose outcome it could not tell."""
    with recording() as record:
        try:
            result = await coro
        except BaseException as err:
            # Also when the run stops later (an abort, a cancel): what may be on the tracker is what the job says.
            if record.unknown_outcomes:
                raise _unknown(record.unknown_outcomes, err) from err
            raise
    if record.unknown_outcomes:
        raise _unknown(record.unknown_outcomes)
    return result


async def _in_click_context(coro: Coroutine[Any, Any, Any]) -> Any:
    # Click's context stack is per thread; tracker pools close when this context exits.
    async with click.Context(click.Command("job")):
        return await coro


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _leaves(error: BaseException) -> list[BaseException]:
    """An exception group's exceptions, nested ones included; or the exception itself."""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for each in error.exceptions for leaf in _leaves(each)]
    return [error]


def _unknown_outcome(error: BaseException) -> UnknownOutcomeError | None:
    """The first UnknownOutcomeError behind `error`: itself, in a group, or anywhere among its causes and contexts."""
    pending: list[BaseException] = [error]
    walked: set[int] = set()
    while pending:
        seen = pending.pop()
        if id(seen) in walked:
            continue
        walked.add(id(seen))
        if isinstance(seen, UnknownOutcomeError):
            return seen
        members = seen.exceptions if isinstance(seen, BaseExceptionGroup) else ()
        linked = [*members, seen.__cause__, seen.__context__]
        # Reversed onto the stack: a group's members in order, each one's cause before its context.
        pending.extend(reversed([each for each in linked if each is not None]))
    return None


def _write_traceback(e: BaseException) -> None:
    """`e`'s traceback, its whole chain, on the server's own stderr."""
    # Never to the job's log, and masked: a chained error may repeat a request's URL.
    output.real_stderr().write(egress.masked_text("".join(traceback.format_exception(e))))


def _failure(e: BaseException) -> str:
    """What a failed job's error says; its traceback goes to the server's own stderr."""
    leaves = _leaves(e)
    _write_traceback(e)
    if any(isinstance(leaf, AbortAndDeleteFolder) for leaf in leaves):
        return "Aborted — the album folder was deleted as requested."
    if any(isinstance(leaf, NoAnswerError) for leaf in leaves):
        return no_answer()
    if any(isinstance(leaf, click.Abort) for leaf in leaves):
        return "Aborted."
    if len(leaves) > 1 or isinstance(e, BaseExceptionGroup):
        return "; ".join(f"{type(leaf).__name__}: {leaf}" for leaf in leaves)
    return f"{type(e).__name__}: {e}"


def _check_folder(job: Job) -> None:
    """Start a job that waited only on its request's folder: still at its real path, and still the same folder."""
    # Validated when the job was made: one swapped since, for a link or another folder, could lead outside the roots.
    if job.folder_path is None:
        return
    if os.path.realpath(job.folder_path) != job.folder_path or _folder_key(job.folder_path) != job.folder:
        raise FolderChangedError(f"{job.folder_path} changed or moved while the job waited: start it again.")


def _outcome(e: BaseException, cancelled: bool) -> tuple[str, str | None]:
    """A job's status and error once it raised `e`: an unknown outcome first, whether or not it was cancelled."""
    unknown = _unknown_outcome(e)
    if unknown is not None:
        _write_traceback(e)
        return "unknown_outcome", f"{UNKNOWN_OUTCOME} ({unknown})"
    if cancelled or all(isinstance(leaf, asyncio.CancelledError) for leaf in _leaves(e)):
        return "cancelled", None
    return "failed", _failure(e)


def _folder_key(folder: str) -> tuple[int, int] | str:
    """The folder as the disk knows it: another spelling of it (a symlink, case, Unicode form) is the same."""
    try:
        found = os.stat(folder)
    except OSError:
        return os.path.realpath(folder)
    return (found.st_dev, found.st_ino)


def _shown(value: Any) -> Any:
    """A job's params as the API shows them: each http(s) URL among them masked, the rest as they are."""
    if isinstance(value, str):
        # By its scheme, not a parse: a URL too malformed to parse is masked whole, never shown.
        return redact_url(value) if value.strip().lower().startswith(("http://", "https://")) else value
    if isinstance(value, dict):
        return {key: _shown(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_shown(item) for item in value]
    return value


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
        self.lock_key: str | None = None
        # The album folder it works on (see _folder_key): a job on the same folder waits until this one ends.
        self.folder: tuple[int, int] | str | None = None
        # Where that folder really is: a job that waited starts only if it is still there.
        self.folder_path: str | None = None
        self.question: dict[str, Any] | None = None
        self.log_lines: list[str] = []
        self.interaction: WebInteraction | None = None
        self.thread: threading.Thread | None = None
        # Run when the job is dropped from the list, so a job that left files
        # behind can take them with it. Set by whoever created those files.
        self.on_evict: Callable[[Job], None] | None = None
        # Reached from the job's thread too.
        self._lock = threading.Lock()
        self._thread_loop: asyncio.AbstractEventLoop | None = None
        self._thread_task: asyncio.Task | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "params": _shown(self.params),
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
        # The jobs waiting their turn, in the order they came, each with the thread it is to run in.
        self._queue: dict[Job, threading.Thread] = {}
        self._running: set[Job] = set()

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

    def create_threaded(
        self,
        job_type: str,
        title: str,
        factory: Callable[[Job], Coroutine[Any, Any, Any]],
        params: dict[str, Any] | None = None,
        lock_key: str | None = None,
        folder: str | None = None,
    ) -> Job:
        """Queue a job to run in a worker thread with its own event loop, once its turn comes.

        A job on `folder` waits while another works on it; a `lock_key` an unfinished job holds refuses this one.
        """
        if len(self._queue) >= MAX_QUEUED_JOBS:
            raise JobCapacityError(f"{MAX_QUEUED_JOBS} jobs are already waiting: try again once some have run.")
        job = self._register(job_type, title, params, lock_key)
        job.folder = _folder_key(folder) if folder else None
        job.folder_path = os.path.realpath(folder) if folder else None
        interaction = WebInteraction(
            emit=lambda event: self._emit_for(job, event),
            set_question=lambda question: self._set_question(job, question),
        )
        job.interaction = interaction
        self._queue[job] = threading.Thread(target=self._thread_main, args=(job, interaction, factory), daemon=True)
        self._broadcast({"event": "created", "job": job.to_dict()})
        self._schedule()
        return job

    def _schedule(self) -> None:
        """Start the jobs waiting, in order, while fewer than MAX_JOBS run; a job waits while its folder is busy."""
        for job, thread in list(self._queue.items()):
            if len(self._running) >= MAX_JOBS:
                return
            if job.folder is not None and any(other.folder == job.folder for other in self._running):
                continue
            del self._queue[job]
            self._running.add(job)
            self._set_status(job, "running")
            job.thread = thread
            try:
                thread.start()
            except RuntimeError:
                # No thread to be had for this job: it ends, and its slot goes to the next.
                job.status = "failed"
                job.error = "Failed to start the worker thread."
                self._running.discard(job)
                self._finish(job)
                self._broadcast({"event": "finished", "job": job.to_dict()})
                if job.interaction is not None:
                    job.interaction.close()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _set_status(self, job: Job, status: str) -> None:
        job.status = status
        self._broadcast({"event": "status", "job_id": job.id, "status": status})

    def _thread_main(
        self, job: Job, interaction: WebInteraction, factory: Callable[[Job], Coroutine[Any, Any, Any]]
    ) -> None:
        interaction_token = set_interaction(interaction)
        progress_token = set_progress_callback(_make_progress_callback(job, self._emit))
        # A BaseEventLoop: its shutdown_default_executor takes the timeout AbstractEventLoop's stub leaves out.
        loop = cast("asyncio.BaseEventLoop", asyncio.new_event_loop())
        job._thread_loop = loop
        # Set on the server's loop once the workers the job left have ended, as the job holds its slot till then.
        status, error, result = "failed", None, None
        try:
            _check_folder(job)
            with job._lock:
                if interaction.cancel_requested.is_set():
                    raise asyncio.CancelledError()
                # Before the task is made: it takes the context, and with it where its output goes and the cancel its
                # tracker requests see.
                with output.to(interaction.lines.write), accounts.cancelled_with(interaction.cancel_requested):
                    task = loop.create_task(_in_click_context(_recorded(factory(job))))
                job._thread_task = task
            result = loop.run_until_complete(task)
            status = "done"
        except BaseException as e:  # noqa: BLE001 - single funnel for job errors
            status, error = _outcome(e, interaction.cancel_requested.is_set())
        finally:
            reset_progress_callback(progress_token)
            set_interaction(None)
            del interaction_token
            # The task is over: a question now could only come from a worker it left, and none would answer it.
            interaction.cancel_pending()
            # As asyncio.run does: the to_thread workers it left end first, so the job holds its folder till then.
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_default_executor(WORKER_JOIN_TIMEOUT))
            with contextlib.suppress(Exception):
                loop.close()
            job._thread_loop = None
            job.question = None
            # The log is whole before the job reads as finished.
            interaction.lines.flush()
            self._call(self._finished, job, status, error, result)
            # From here on, leaked to_thread workers must neither emit
            # events nor block on unanswerable questions.
            interaction.close()

    def _finish(self, job: Job) -> None:
        if job.lock_key is not None:
            self._active_lock_keys.discard(job.lock_key)
        job.finished_at = _now()

    def _finished(self, job: Job, status: str, error: str | None, result: Any) -> None:
        """On the server's loop, once a job's thread has ended: its outcome, and the jobs it held back started."""
        job.status, job.error, job.result = status, error, result
        self._running.discard(job)
        self._finish(job)
        self._broadcast({"event": "finished", "job": job.to_dict()})
        self._schedule()

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

    def _call(self, callback: Callable[..., None], *args: Any) -> None:
        """Run `callback` on the server's loop, from any thread."""
        loop = self._server_loop
        if loop is None:
            return
        # RuntimeError: the server loop already closed.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(callback, *args)

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

    async def stop(self, timeout: float | None = None) -> bool:
        """Cancel every job, and wait for the running ones to end, up to STOP_TIMEOUT seconds; False if one did not."""
        for job in [*self._queue, *self._running]:
            self.cancel(job.id)
        try:
            async with asyncio.timeout(STOP_TIMEOUT if timeout is None else timeout):
                while self._running:
                    await asyncio.sleep(0.05)
        except TimeoutError:
            names = ", ".join(sorted(job.id for job in self._running))
            print(f"salmon web: stopped while jobs were still running: {names}", file=output.real_stderr())
        return not self._running

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status in FINISHED:
            return False
        if job in self._queue:
            del self._queue[job]
            job.status = "cancelled"
            self._finish(job)
            self._broadcast({"event": "finished", "job": job.to_dict()})
            if job.interaction is not None:
                job.interaction.close()
            return True
        if job.interaction is not None:
            job.interaction.cancel_pending()
        with job._lock:
            loop, task = job._thread_loop, job._thread_task
        if loop is not None and task is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
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
        finished = [j for j in self.jobs.values() if j.status in FINISHED]
        if len(finished) <= MAX_FINISHED_JOBS:
            return
        for job in finished[: len(finished) - MAX_FINISHED_JOBS]:
            if job.on_evict is not None:
                # Never let housekeeping take the manager down with it.
                with contextlib.suppress(Exception):
                    job.on_evict(job)
            del self.jobs[job.id]


manager = JobManager()
