"""What every client of one tracker account shares on one event loop: its connection pool.

A tracker limits the account, not a client object, and a command may hold several clients of one
tracker (ADR 0004, amending ADR 0001). Each tracker has its own. The pool is kept per event loop, as
an aiohttp session cannot be used from another loop; the rate limit and the lock for requests that
are not idempotent are kept process-wide (`salmon.trackers.base`).

salmon web runs each job on a loop of its own, so that loop's pool would be the job's only. While it
runs, every tracker request is handed over to its loop instead (`requests_on_this_loop`), so all jobs
share one pool per account. In the CLI nothing is handed over.
"""

import asyncio
import contextvars
import threading
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TypeVar
from weakref import WeakSet

import anyio
import asyncclick as click

from salmon.errors import RequestFailedError, UnknownOutcomeError

if TYPE_CHECKING:
    import aiohttp

T = TypeVar("T")

# Kept-alive connections in an account's pool (ADR 0001).
POOL_CONNECTIONS = 2


class TrackerAccount:
    """The state every client of one tracker shares on one event loop."""

    def __init__(self) -> None:
        self.pool: aiohttp.ClientSession | None = None
        # The clients using the pool. The last one to be closed closes it.
        self.clients: WeakSet[object] = WeakSet()


_accounts: dict[asyncio.AbstractEventLoop, dict[str, TrackerAccount]] = {}
# Two threads, each with its own loop, may look up their accounts at once.
_accounts_lock = threading.Lock()


def account_for(site_code: str) -> TrackerAccount:
    """The account of the tracker `site_code` on the running event loop, made on first use."""
    loop = asyncio.get_running_loop()
    with _accounts_lock:
        # A closed loop sends nothing again; its accounts would otherwise be kept for good.
        for closed in [other for other in _accounts if other.is_closed()]:
            del _accounts[closed]
        accounts = _accounts.setdefault(loop, {})
        if site_code not in accounts:
            accounts[site_code] = TrackerAccount()
        return accounts[site_code]


async def _close_pools(loop: asyncio.AbstractEventLoop) -> None:
    """Close the pools opened on `loop`, the running one, and forget its accounts."""
    with _accounts_lock:
        accounts = _accounts.pop(loop, {})
    for account in accounts.values():
        pool, account.pool = account.pool, None
        if pool is not None:
            await pool.close()


# The loop every tracker request runs on while salmon web runs, whichever loop makes it. None in the CLI,
# where a command has one loop and its requests run where they are made.
_request_loop: asyncio.AbstractEventLoop | None = None


class _HandOver:
    """A call handed over to the request loop: its task there, and whether a cancel may still stop it."""

    def __init__(self, cancellable: bool) -> None:
        self.task: asyncio.Task | None = None
        self.cancellable = cancellable
        # Set once the attempt in flight may have reached the tracker: a cancel then waits for it to end.
        self.committed = not cancellable
        # Set once the caller was cancelled: no new attempt goes out. A redirect hop of the attempt in flight
        # still does, as the tracker has acted on it.
        self.cancel_requested = False


_hand_over: ContextVar[_HandOver | None] = ContextVar("tracker_hand_over", default=None)
# A salmon web job's cancel: an Event, as a task the job starts copies the context it is made in.
_job_cancel: ContextVar[threading.Event | None] = ContextVar("tracker_job_cancel", default=None)


@asynccontextmanager
async def requests_on_this_loop() -> AsyncIterator[None]:
    """Run every tracker request made during the block on the running loop, whichever loop makes it.

    For salmon web, whose jobs each run on a loop of their own (ADR 0004). The pools opened on this loop
    are closed when the block ends.
    """
    global _request_loop
    if _request_loop is not None:
        raise RuntimeError("tracker requests already run on another loop")
    loop = asyncio.get_running_loop()
    _request_loop = loop
    try:
        yield
    finally:
        _request_loop = None
        await _close_pools(loop)


def handing_over() -> bool:
    """Whether a tracker request made here runs on another loop: salmon web's, from a job's own loop."""
    loop = _request_loop
    return loop is not None and loop is not asyncio.get_running_loop()


def on_request_loop() -> bool:
    """Whether the running loop is salmon web's request loop, which closes its pools when it stops."""
    loop = _request_loop
    return loop is not None and loop is asyncio.get_running_loop()


def commit() -> None:
    """Note that the call handed over may reach the tracker from now on: a cancel no longer stops it.

    Called right before a request that is not idempotent goes out. Does nothing in a call not handed over.
    """
    hand_over = _hand_over.get()
    if hand_over is not None:
        hand_over.committed = True


def begin_attempt() -> None:
    """Start an attempt of a handed-over call: nothing of it has gone out, and none goes if its caller was cancelled.

    Called before each retry, ahead of the wait for it. A retry is only sent when the tracker did not act on the
    attempt before, so the call can stop there with nothing done, and a cancel stops it at once until it has its
    turn (see `commit`). Not called on a redirect hop, whose request the tracker has acted on: that one runs to
    its end, and its outcome reaches the caller.

    Raises:
        asyncio.CancelledError: If the caller was cancelled.
    """
    hand_over = _hand_over.get()
    if hand_over is None:
        return
    hand_over.committed = not hand_over.cancellable
    if hand_over.cancel_requested:
        raise asyncio.CancelledError("cancelled before sending the request again")


@contextmanager
def cancelled_with(event: threading.Event) -> Iterator[None]:
    """Count the tasks made in the block, and those they start, as cancelled once `event` is set: a job's cancel."""
    token = _job_cancel.set(event)
    try:
        yield
    finally:
        _job_cancel.reset(token)


def caller_cancelled() -> bool:
    """Whether the job here was cancelled and went on, as one whose cancel came back as an unknown outcome does.

    Seen from any task of the job, also one started after the cancel (see `cancelled_with`).
    """
    event = _job_cancel.get()
    return event is not None and event.is_set()


def own_context() -> contextvars.Context:
    """A copy of this context outside any handed-over call: for work several callers share, none of whose cancels
    may stop it for the others."""
    context = contextvars.copy_context()
    context.run(_hand_over.set, None)
    return context


def _settle(finished: asyncio.Future[asyncio.Task], task: asyncio.Task) -> None:
    if not finished.done():
        finished.set_result(task)


async def run_on_request_loop(
    call: Callable[[], Coroutine[Any, Any, T]], *, cancellable: bool = True, what: str = ""
) -> T:
    """Run `call()` on the request loop, in a copy of this context, and return what it returns.

    The copy carries the caller's context variables (a dry run, a job's output and its interaction)
    to the request. Cancelling the caller cancels the call at once, unless it has committed (see
    `commit`): the caller then waits for it to end, and is cancelled after. An UnknownOutcomeError it
    raises still reaches the caller, so its outcome is never lost.

    Args:
        call: Makes the coroutine to run there.
        cancellable: False for a call no cancel may stop part way.
        what: What the call sends, for the message shown when a cancel came after it went out.

    Raises:
        RequestFailedError: If salmon web stopped before the call could run or end.
    """
    loop = _request_loop
    if loop is None or loop is asyncio.get_running_loop():
        return await call()
    context = contextvars.copy_context()
    hand_over = _HandOver(cancellable)
    context.run(_hand_over.set, hand_over)
    caller_loop = asyncio.get_running_loop()
    finished: asyncio.Future[asyncio.Task] = caller_loop.create_future()

    def tell_caller(task: asyncio.Task) -> None:
        # A closed loop has no one waiting on it any more.
        with suppress(RuntimeError):
            caller_loop.call_soon_threadsafe(_settle, finished, task)

    def start() -> None:
        hand_over.task = loop.create_task(call(), context=context)
        hand_over.task.add_done_callback(tell_caller)

    def cancel_unless_committed() -> None:
        hand_over.cancel_requested = True
        if hand_over.task is not None and not hand_over.committed:
            hand_over.task.cancel()

    try:
        loop.call_soon_threadsafe(start)
    except RuntimeError as err:
        raise RequestFailedError("salmon web is stopping: the request was not sent") from err

    cancelled: asyncio.CancelledError | None = None
    try:
        await asyncio.shield(finished)
    except asyncio.CancelledError as err:
        # Raised again once the call has ended: anyio knows its own cancellations by this one's message.
        cancelled = err
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(cancel_unless_committed)
        # Shielded from anyio too, which cancels a task again on each step until it ends.
        with anyio.CancelScope(shield=True):
            while not finished.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(finished)
    return _outcome(finished.result(), hand_over, cancelled, what)


def _outcome(task: asyncio.Task, hand_over: _HandOver, cancelled: asyncio.CancelledError | None, what: str) -> Any:
    """What the caller of a handed-over call gets once the call has ended: its result, or what it raised."""
    if task.cancelled() and hand_over.committed and what:
        # Cut off once it may have gone out, as by salmon web stopping, whether or not the caller was cancelled.
        raise UnknownOutcomeError(f"salmon web stopped before {what} was answered")
    if cancelled is None:
        if task.cancelled():
            # Only salmon web stopping cancels a call no one cancelled.
            if hand_over.committed:
                raise RequestFailedError("salmon web stopped")
            raise RequestFailedError("salmon web stopped: the request was not sent")
        return task.result()
    if not task.cancelled():
        error = task.exception()
        if isinstance(error, UnknownOutcomeError):
            raise error
        if hand_over.committed and what:
            click.secho(f"Cancelled once {what} had been answered: check the site for what it did.", fg="yellow")
    raise cancelled
