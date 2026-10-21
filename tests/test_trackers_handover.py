"""salmon web runs each job on a loop of its own, and every tracker request on its one request loop (#631, ADR 0004).

The jobs here are threads, each running a loop of its own as salmon web's jobs do, sending through a local fake
tracker served on the test's main loop, which is the request loop. Most tests shorten the rate limit's period to
one second, which changes the times only, not the rule.
"""

import asyncio
import contextvars
import gc
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from tenacity import wait_fixed, wait_none
from test_trackers_account import (  # pyright: ignore[reportMissingImports]
    HELD,
    POST_TIME,
    FakeApi,
    FakeTracker,
    _clients,
    _get,
    _most_within,
    _post,
)

from salmon import cfg, dryrun
from salmon.errors import DryRunRefused, RequestFailedError, UnknownOutcomeError
from salmon.trackers import account, base
from salmon.trackers.base import BaseGazelleApi
from salmon.webui import output

# Read by the code a request runs on the request loop: the job's value must arrive there.
JOB_NAME: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_name", default=None)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without the backoff between attempts, so only the rate limit waits are left."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]
    # The debug lines would go to each job's output as well.
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", False)


@pytest.fixture
def short_period(monkeypatch: pytest.MonkeyPatch) -> float:
    """A rate limit period of one second, with a margin as large for it as the real one is for 10 s."""
    monkeypatch.setattr(base, "_RATE_PERIOD", 1.2)
    return 1.0


class Tracker(FakeTracker):
    """The fake tracker, which also answers a POST with action "fail" 500, one with "busy" 429, and one with "hold"
    after a second."""

    def __init__(self) -> None:
        super().__init__()
        # The requests that are not idempotent the fake has finished answering, by action.
        self.answered: list[str] = []
        self.arrived = asyncio.Event()

    async def handle(self, request: web.Request) -> web.Response:
        self.arrived.set()
        action = request.query.get("action", "")
        if request.method == "POST" and action in ("fail", "busy", "hold"):
            await request.read()
            self.hits.append((time.monotonic(), request.headers["X-Site"], request.method, action))
            await asyncio.sleep(HELD if action == "hold" else POST_TIME)
            self.answered.append(action)
            if action == "hold":
                return web.json_response({"status": "success", "response": {}})
            if action == "busy":
                return web.json_response(
                    {"status": "failure", "error": "Rate limit exceeded"}, status=429, headers={"Retry-After": "0"}
                )
            return web.json_response({"status": "failure", "error": "Server error"}, status=500)
        response = await super().handle(request)
        if request.method == "POST":
            self.answered.append(action)
        return response


class Job:
    """A coroutine run on a loop of its own in a thread of its own, which the test can cancel."""

    def __init__(self, run: Callable[[], Awaitable[Any]], name: str = "") -> None:
        self._run = run
        self.name = name
        self.result: Any = None
        self.error: BaseException | None = None
        self.started = threading.Event()
        self.ended_at = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._cancelled = threading.Event()
        self.thread = threading.Thread(target=self._main)

    def _main(self) -> None:
        async def main() -> Any:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            JOB_NAME.set(self.name)
            self.started.set()
            return await self._run()

        try:
            with account.cancelled_with(self._cancelled):
                self.result = asyncio.run(main())
        except BaseException as err:
            self.error = err
        self.ended_at = time.monotonic()

    def cancel(self) -> None:
        # As salmon web's JobManager.cancel: the job's tasks see the cancel, then its task is cancelled.
        assert self._loop is not None
        assert self._task is not None
        self._cancelled.set()
        self._loop.call_soon_threadsafe(self._task.cancel)


async def _in_threads(*jobs: Job) -> None:
    """Start the jobs and wait, without blocking the request loop, until each has ended."""
    for job in jobs:
        job.thread.start()
    for job in jobs:
        await anyio.to_thread.run_sync(job.thread.join, 30)
        assert not job.thread.is_alive()


def _serve(body: Callable[[Tracker], Awaitable[None]], *, request_loop: bool = True) -> Tracker:
    """Run the fake tracker on the test's loop, made the request loop, and fail on any session left open."""
    unclosed: list[str] = []
    tracker = Tracker()

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        try:
            if request_loop:
                async with account.requests_on_this_loop():
                    await body(tracker)
            else:
                await body(tracker)
        finally:
            await tracker.runner.cleanup()
        gc.collect()
        await anyio.sleep(0.05)

    anyio.run(main)
    assert unclosed == []
    assert account._request_loop is None
    return tracker


def _record_loops(monkeypatch: pytest.MonkeyPatch) -> list[tuple[asyncio.AbstractEventLoop, str | None]]:
    """Note the loop each request is sent on, and the job's name as the request sees it."""
    seen: list[tuple[asyncio.AbstractEventLoop, str | None]] = []
    send = BaseGazelleApi._send

    async def recording(self: BaseGazelleApi, *args: Any, **kwargs: Any) -> Any:
        seen.append((asyncio.get_running_loop(), JOB_NAME.get()))
        return await send(self, *args, **kwargs)

    monkeypatch.setattr(BaseGazelleApi, "_send", recording)
    return seen


# --- Where requests run ------------------------------------------------------


def test_without_a_request_loop_a_request_runs_on_the_loop_that_makes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI: no loop is registered, nothing is handed over.
    seen = _record_loops(monkeypatch)
    loops: list[asyncio.AbstractEventLoop] = []

    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED")

        async def job() -> None:
            loops.append(asyncio.get_running_loop())
            await _get(api)
            await api.close()

        await _in_threads(Job(job))
        loops.append(asyncio.get_running_loop())
        await _get(api)
        await api.close()

    _serve(body, request_loop=False)
    assert [loop for loop, _ in seen] == loops


def test_a_request_from_a_job_runs_on_the_request_loop_with_the_jobs_context(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_loops(monkeypatch)
    request_loop: list[asyncio.AbstractEventLoop] = []

    async def body(tracker: Tracker) -> None:
        request_loop.append(asyncio.get_running_loop())
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)

        async def job() -> None:
            await _get(api)
            await api.close()

        await _in_threads(Job(job, "A"), Job(job, "B"))

    tracker = _serve(body)
    assert len(tracker.hits) == 2
    assert {loop for loop, _ in seen} == set(request_loop)
    assert sorted(name or "" for _, name in seen) == ["A", "B"]


def test_a_dry_run_job_is_refused_its_post_while_another_job_sends_its_own() -> None:
    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)

        async def dry() -> None:
            with dryrun.mode():
                await _post(api)

        async def real() -> None:
            await _post(api)

        jobs = Job(dry), Job(real)
        await _in_threads(*jobs)
        await api.close()
        assert isinstance(jobs[0].error, DryRunRefused)
        assert jobs[1].error is None

    tracker = _serve(body)
    assert [method for _, _, method, _ in tracker.hits] == ["POST"]


def test_what_a_request_prints_goes_to_the_job_that_sent_it(short_period: float) -> None:
    # The 429's message goes to the log of the job whose request got it, not to the other job's.
    logs: dict[str, list[str]] = {"limited": [], "other": []}

    async def body(tracker: Tracker) -> None:
        limited, other = _clients(await tracker.start(), "RED", "RED")
        tracker.limit_once["limited"] = "2"

        def logging(name: str, api: FakeApi, action: str) -> Callable[[], Awaitable[None]]:
            async def job() -> None:
                lines = output.Lines(lambda line, _err: logs[name].append(line))
                with output.to(lines.write):
                    await _get(api, action)
                lines.flush()

            return job

        with output.capturing():
            await _in_threads(Job(logging("limited", limited, "limited")), Job(logging("other", other, "other")))
        await limited.close()
        await other.close()

    _serve(body)
    assert logs == {"limited": ["Rate limit exceeded, waiting 2 seconds..."], "other": []}


# --- One account across jobs ------------------------------------------------


def test_two_jobs_share_one_budget(short_period: float) -> None:
    async def body(tracker: Tracker) -> None:
        site = await tracker.start()

        def burst() -> Callable[[], Awaitable[None]]:
            async def job() -> None:
                # A client of its own, as each job makes.
                (api,) = _clients(site, "RED")
                await asyncio.gather(*(_get(api) for _ in range(6)))
                await api.close()

            return job

        await _in_threads(Job(burst()), Job(burst()))

    tracker = _serve(body)
    assert len(tracker.hits) == 12
    assert _most_within(tracker.times(), short_period) == base._tracker_limiter("RED")._max_rate


def test_two_jobs_send_one_request_that_is_not_idempotent_at_a_time() -> None:
    async def body(tracker: Tracker) -> None:
        site = await tracker.start()

        async def job() -> None:
            (api,) = _clients(site, "RED", unlimited=True)
            await asyncio.gather(_post(api), _post(api))
            await api.close()

        await _in_threads(Job(job), Job(job))

    tracker = _serve(body)
    assert [method for _, _, method, _ in tracker.hits] == ["POST"] * 4
    assert tracker.peak_posts == 1


def test_two_jobs_share_one_pool() -> None:
    async def body(tracker: Tracker) -> None:
        site = await tracker.start()

        async def job() -> None:
            (api,) = _clients(site, "RED", unlimited=True)
            await asyncio.gather(*(_get(api, "slow") for _ in range(3)), _post(api))
            await api.close()

        await _in_threads(Job(job), Job(job), Job(job))

    tracker = _serve(body)
    assert len(tracker.hits) == 3 * 4
    # The pool's two connections, and one for the request that is not idempotent in flight.
    assert tracker.peak_connections <= account.POOL_CONNECTIONS + 1
    # Every job's GETs through the same two connections; each POST on one of its own.
    assert len(tracker.transports) == account.POOL_CONNECTIONS + 3


def test_a_429_to_one_job_holds_the_other_jobs_requests() -> None:
    wait = 3

    async def body(tracker: Tracker) -> None:
        site = await tracker.start()
        tracker.limit_once["limited"] = str(wait)
        (limited,) = _clients(site, "RED")
        (other,) = _clients(site, "RED")

        async def first() -> None:
            await _get(limited, "limited")
            await limited.close()

        async def second() -> None:
            # Once the 429 has reached the other job.
            while not tracker.limited.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            await _get(other, "other")
            await other.close()

        await _in_threads(Job(first), Job(second))

    tracker = _serve(body)
    (answered_429, *later) = tracker.hits
    assert sorted(action for _, _, _, action in later) == ["limited", "other"]
    assert all(at - answered_429[0] >= wait - 0.05 for at, _, _, _ in later)


def test_a_job_authenticates_on_the_request_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_loops(monkeypatch)
    request_loop: list[asyncio.AbstractEventLoop] = []
    authenticated_on: list[asyncio.AbstractEventLoop] = []

    async def body(tracker: Tracker) -> None:
        request_loop.append(asyncio.get_running_loop())
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)
        api._authenticated = False

        async def authenticate() -> None:
            authenticated_on.append(asyncio.get_running_loop())
            api.authkey, api.passkey = "an-authkey", "a-passkey"
            api._authenticated = True

        monkeypatch.setattr(api, "authenticate", authenticate)

        async def job() -> None:
            await api.ensure_authenticated()
            await _get(api)
            await api.close()

        await _in_threads(Job(job))
        assert api._authenticated

    _serve(body)
    assert [loop for loop, _ in seen] == request_loop
    # The attempt is a task of the request loop, which every job's client may wait on.
    assert authenticated_on == request_loop


def test_a_job_closing_the_last_client_leaves_the_pool_open_for_the_next_job() -> None:
    pools: list[Any] = []

    async def body(tracker: Tracker) -> None:
        site = await tracker.start()
        first, later = _clients(site, "RED", "RED", unlimited=True)

        async def job() -> None:
            await _get(first)
            pools.append(first._session)
            await first.close()

        async def next_job() -> None:
            await _get(later)
            pools.append(later._session)

        await _in_threads(Job(job))
        assert pools[0] is not None
        assert not pools[0].closed
        await _in_threads(Job(next_job))
        # The next job's client goes out on the same pool.
        assert pools[1] is pools[0]

    _serve(body)
    # Closed when the request loop stopped.
    assert pools[0].closed


def test_closing_a_jobs_client_leaves_the_pool_to_the_others_and_salmon_web_closes_it() -> None:
    pools: list[Any] = []

    async def body(tracker: Tracker) -> None:
        first, second = _clients(await tracker.start(), "RED", "RED", unlimited=True)

        async def job() -> None:
            await asyncio.gather(_get(first), _get(second))
            await first.close()

        await _in_threads(Job(job))
        pools.append(second._session)
        assert second._session is not None
        assert not second._session.closed

    _serve(body)
    # Never closed by the second client: closed when the request loop stopped.
    assert pools[0].closed


# --- Cancelling a job --------------------------------------------------------


def test_cancelling_a_job_during_a_get_cancels_the_request_at_once() -> None:
    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)

        async def job() -> None:
            await _get(api, "held")

        each = Job(job)
        each.thread.start()
        await tracker.arrived.wait()
        cancelled_at = time.monotonic()
        each.cancel()
        await anyio.to_thread.run_sync(each.thread.join, 30)
        assert isinstance(each.error, asyncio.CancelledError)
        # Long before the fake's answer (1 s).
        assert each.ended_at - cancelled_at < 0.5
        await api.close()

    _serve(body)


def test_cancelling_a_job_during_a_post_waits_for_the_trackers_answer(capsys: pytest.CaptureFixture[str]) -> None:
    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)
        after: list[str] = []

        async def job() -> None:
            await _post(api)
            after.append("went on")

        each = Job(job)
        each.thread.start()
        await tracker.arrived.wait()
        arrived_at = time.monotonic()
        each.cancel()
        await anyio.to_thread.run_sync(each.thread.join, 30)
        assert isinstance(each.error, asyncio.CancelledError)
        # The job stopped once the tracker had answered, and went no further.
        assert tracker.answered == ["upload"]
        assert each.ended_at - arrived_at >= POST_TIME - 0.05
        assert after == []
        await api.close()

    tracker = _serve(body)
    assert [method for _, _, method, _ in tracker.hits] == ["POST"]
    assert "Cancelled once POST /ajax.php?action=upload to RED had been answered" in capsys.readouterr().out


def test_cancelling_a_job_whose_post_waits_for_its_turn_sends_nothing() -> None:
    # Another job's POST holds the account's lock: the cancelled one has not gone out yet.
    async def body(tracker: Tracker) -> None:
        site = await tracker.start()
        (first,) = _clients(site, "RED", unlimited=True)
        (second,) = _clients(site, "RED", unlimited=True)

        async def hold() -> None:
            await first._request("POST", site + "/ajax.php", params={"action": "hold"}, data={"file": "x"})

        async def post() -> None:
            await _post(second)

        holding = Job(hold)
        waiting = Job(post)
        holding.thread.start()
        await tracker.arrived.wait()
        waiting.thread.start()
        await anyio.to_thread.run_sync(waiting.started.wait, 5)
        await asyncio.sleep(0.05)
        cancelled_at = time.monotonic()
        waiting.cancel()
        for each in (holding, waiting):
            await anyio.to_thread.run_sync(each.thread.join, 30)
        assert holding.error is None
        assert isinstance(waiting.error, asyncio.CancelledError)
        # Long before the other POST is answered (1 s).
        assert waiting.ended_at - cancelled_at < 0.5
        await first.close()
        await second.close()

    tracker = _serve(body)
    assert [method for _, _, method, _ in tracker.hits] == ["POST"]


def test_a_cancelled_jobs_post_the_tracker_did_not_act_on_is_not_sent_again() -> None:
    # The POST had its turn, so the cancel waits for it; the tracker answers 429, which it sends without acting.
    # The retry would send the POST the user cancelled.
    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)
        tracker.limit_once["upload"] = "2"

        async def job() -> None:
            await _post(api)

        each = Job(job)
        each.thread.start()
        await tracker.limited.wait()
        each.cancel()
        await anyio.to_thread.run_sync(each.thread.join, 30)
        assert isinstance(each.error, asyncio.CancelledError)
        await api.close()

    tracker = _serve(body)
    assert [(method, action) for _, _, method, action in tracker.hits] == [("POST", "upload")]


def test_a_cancelled_job_still_gets_the_unknown_outcome_of_its_post() -> None:
    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)

        async def job() -> None:
            await api._request("POST", api.base_url + "/ajax.php", params={"action": "fail"}, data={"file": "x"})

        each = Job(job)
        each.thread.start()
        await tracker.arrived.wait()
        each.cancel()
        await anyio.to_thread.run_sync(each.thread.join, 30)
        assert isinstance(each.error, UnknownOutcomeError)
        await api.close()

    tracker = _serve(body)
    # Never sent again.
    assert [(method, action) for _, _, method, action in tracker.hits] == [("POST", "fail")]


def test_a_job_gets_the_unknown_outcome_of_its_post_and_nothing_is_sent_again() -> None:
    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)

        async def job() -> None:
            await api._request("POST", api.base_url + "/ajax.php", params={"action": "fail"}, data={"file": "x"})

        each = Job(job)
        await _in_threads(each)
        assert isinstance(each.error, UnknownOutcomeError)
        await api.close()

    tracker = _serve(body)
    assert [(method, action) for _, _, method, action in tracker.hits] == [("POST", "fail")]


def test_a_cancel_while_a_posts_retry_waits_for_its_turn_stops_it_at_once() -> None:
    # The POST is answered 429, which the tracker sends without acting: its retry waits 2 s for its turn, and the
    # cancel during that wait stops it there.
    stopped_after: list[float] = []

    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)
        tracker.limit_once["upload"] = "2"

        async def job() -> None:
            await _post(api)

        each = Job(job)
        each.thread.start()
        await tracker.limited.wait()
        await asyncio.sleep(0.5)
        cancelled_at = time.monotonic()
        each.cancel()
        await anyio.to_thread.run_sync(each.thread.join, 30)
        assert isinstance(each.error, asyncio.CancelledError)
        stopped_after.append(each.ended_at - cancelled_at)
        await api.close()

    tracker = _serve(body)
    assert [(method, action) for _, _, method, action in tracker.hits] == [("POST", "upload")]
    # Long before the retry's turn (1.5 s on).
    assert stopped_after[0] < 0.5


@pytest.mark.parametrize(
    "cancel_after", [0.1, POST_TIME + 0.2], ids=["while_the_429_is_on_its_way", "during_the_backoff_after_it"]
)
def test_a_cancel_around_a_429_to_a_post_stops_it_before_the_backoff_ends(
    monkeypatch: pytest.MonkeyPatch, cancel_after: float
) -> None:
    # A 429 says the tracker did not act: once it is answered nothing of the POST is out, and the cancel stops the job.
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_fixed(3))  # type: ignore[attr-defined]
    stopped_after: list[float] = []

    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)

        async def job() -> None:
            await api._request("POST", api.base_url + "/ajax.php", params={"action": "busy"}, data={"file": "x"})

        each = Job(job)
        each.thread.start()
        await tracker.arrived.wait()
        await asyncio.sleep(cancel_after)
        cancelled_at = time.monotonic()
        each.cancel()
        await anyio.to_thread.run_sync(each.thread.join, 30)
        assert isinstance(each.error, asyncio.CancelledError)
        stopped_after.append(each.ended_at - cancelled_at)
        await api.close()

    tracker = _serve(body)
    assert [(method, action) for _, _, method, action in tracker.hits] == [("POST", "busy")]
    # Long before the backoff's end (3 s after the 429).
    assert stopped_after[0] < 1


@pytest.mark.parametrize("from_a_task_it_starts", [False, True], ids=["itself", "from_a_task_it_starts"])
def test_a_cancelled_job_that_goes_on_past_an_unknown_outcome_looks_up_but_changes_nothing(
    from_a_task_it_starts: bool,
) -> None:
    # As the upload does, which looks a lost upload up by its infohash, and the lossy report and description edit
    # after it: the cancel came back as the unknown outcome, and the job goes on. Image uploads, the tracker's own
    # host's among them, go out from tasks a job starts.
    went_on: list[str] = []

    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)

        async def job() -> None:
            try:
                await api._request("POST", api.base_url + "/ajax.php", params={"action": "fail"}, data={"file": "x"})
            except UnknownOutcomeError:
                went_on.append("caught")
            await _get(api, "torrent")
            went_on.append("looked it up")
            if from_a_task_it_starts:
                await asyncio.ensure_future(_post(api))
            else:
                await _post(api)
            went_on.append("sent the next POST")

        each = Job(job)
        each.thread.start()
        await tracker.arrived.wait()
        each.cancel()
        await anyio.to_thread.run_sync(each.thread.join, 30)
        assert isinstance(each.error, asyncio.CancelledError)
        await api.close()

    tracker = _serve(body)
    assert went_on == ["caught", "looked it up"]
    assert [(method, action) for _, _, method, action in tracker.hits] == [("POST", "fail"), ("GET", "torrent")]


def test_a_sign_in_runs_outside_the_hand_over_of_the_request_that_started_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Shared by every request waiting on it: the first one's cancel must not stop its retries for the others.
    seen: list[object] = []

    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)
        api._authenticated = False

        async def authenticate() -> None:
            seen.append(account._hand_over.get())
            api._authenticated = True

        monkeypatch.setattr(api, "authenticate", authenticate)
        await _in_threads(Job(api.ensure_authenticated))
        await api.close()

    _serve(body)
    assert seen == [None]


def test_a_post_cut_off_by_salmon_web_stopping_after_a_cancel_is_an_unknown_outcome() -> None:
    async def body(tracker: Tracker) -> None:
        await tracker.start()
        sending: list[asyncio.Task] = []
        went_out = asyncio.Event()

        async def call() -> None:
            # As _turn does once the POST has its turn.
            account.commit()
            sending.append(asyncio.current_task())  # type: ignore[arg-type]
            went_out.set()
            await asyncio.sleep(15)

        async def job() -> None:
            await account.run_on_request_loop(call, what="POST /ajax.php?action=upload to RED")

        each = Job(job)
        each.thread.start()
        await went_out.wait()
        each.cancel()
        # The cancel waits for the answer; salmon web stopping cancels the request on its loop.
        await asyncio.sleep(0.1)
        sending[0].cancel()
        await anyio.to_thread.run_sync(each.thread.join, 30)
        assert isinstance(each.error, UnknownOutcomeError)
        assert "salmon web stopped before POST /ajax.php?action=upload to RED was answered" in str(each.error)

    _serve(body)


@pytest.mark.parametrize(
    ("cancellable", "what", "error", "message"),
    [
        (
            True,
            "POST /ajax.php?action=upload to RED",
            RequestFailedError,
            "salmon web stopped: the request was not sent",
        ),
        (False, "", RequestFailedError, "salmon web stopped"),
        (
            False,
            "POST /ajax.php?action=upload to RED",
            UnknownOutcomeError,
            "salmon web stopped before POST /ajax.php?action=upload to RED was answered",
        ),
    ],
    ids=["before_it_went_out", "after_it_may_have_gone_out", "after_an_upload_may_have_gone_out"],
)
def test_salmon_web_stopping_during_a_call_says_whether_it_may_have_gone_out(
    cancellable: bool, what: str, error: type[Exception], message: str
) -> None:
    async def body(tracker: Tracker) -> None:
        await tracker.start()
        running: list[asyncio.Task] = []
        started = asyncio.Event()

        async def call() -> None:
            running.append(asyncio.current_task())  # type: ignore[arg-type]
            started.set()
            await asyncio.sleep(15)

        async def job() -> None:
            await account.run_on_request_loop(call, cancellable=cancellable, what=what)

        each = Job(job)
        each.thread.start()
        await started.wait()
        # No one cancelled the job: salmon web stopping cancels the request on its loop.
        running[0].cancel()
        await anyio.to_thread.run_sync(each.thread.join, 30)
        assert isinstance(each.error, error)
        assert str(each.error) == message

    _serve(body)


def test_an_anyio_cancel_stops_a_get_at_once() -> None:
    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)

        async def job() -> float:
            with anyio.move_on_after(0.1):
                await _get(api, "held")
            return time.monotonic()

        each = Job(job)
        started = time.monotonic()
        await _in_threads(each)
        assert each.error is None
        # Long before the fake's answer (1 s).
        assert each.result - started < 0.8
        await api.close()

    _serve(body)


def test_an_anyio_cancel_during_a_post_waits_for_the_answer_without_spinning() -> None:
    # anyio cancels a task again on each step until it ends: the wait for the answer must not wake up each time.
    async def body(tracker: Tracker) -> None:
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)

        async def post() -> None:
            await _post(api)

        async def job() -> float:
            cpu = time.thread_time()
            async with anyio.create_task_group() as group:
                group.start_soon(post)
                while not tracker.hits:
                    await anyio.sleep(0.01)
                group.cancel_scope.cancel()
            return time.thread_time() - cpu

        each = Job(job)
        await _in_threads(each)
        assert each.error is None
        # A wait woken on each step would keep this thread busy for the POST's 0.3 s.
        assert each.result < POST_TIME / 3
        await api.close()

    tracker = _serve(body)
    assert tracker.answered == ["upload"]


def test_a_request_handed_over_once_salmon_web_has_stopped_is_refused() -> None:
    loop = asyncio.new_event_loop()
    loop.close()

    async def job() -> None:
        account._request_loop = loop
        try:
            await account.run_on_request_loop(lambda: asyncio.sleep(0))
        finally:
            account._request_loop = None

    with pytest.raises(RequestFailedError, match="salmon web is stopping"):
        asyncio.run(job())


def test_only_one_loop_can_be_the_request_loop() -> None:
    async def main() -> None:
        async with account.requests_on_this_loop():
            with pytest.raises(RuntimeError):
                async with account.requests_on_this_loop():
                    pass

    anyio.run(main)
    assert account._request_loop is None


def test_salmon_web_keeps_no_client_of_a_finished_job() -> None:
    # salmon web's command outlives every job: a client registering its close with it would be kept for good.
    clients: list[weakref.ref[FakeApi]] = []

    @click.command()
    async def command() -> None:
        tracker = Tracker()
        site = await tracker.start()
        try:
            async with account.requests_on_this_loop():

                async def job() -> None:
                    (api,) = _clients(site, "RED", unlimited=True)
                    clients.append(weakref.ref(api))
                    await _get(api)

                await _in_threads(Job(job))
                gc.collect()
                assert clients[0]() is None
        finally:
            await tracker.runner.cleanup()

    command(args=[], standalone_mode=False)
    assert len(clients) == 1


def test_the_cli_command_still_closes_its_pool() -> None:
    # Unchanged: in the CLI a client registers its close with the command.
    pools = []

    @click.command()
    async def command() -> None:
        tracker = Tracker()
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)
        try:
            await _get(api)
            pools.append(api._session)
        finally:
            await tracker.runner.cleanup()

    command(args=[], standalone_mode=False)
    assert pools[0] is not None
    assert pools[0].closed
