"""Every client of one tracker account shares one pool of kept-alive connections, on each event loop (ADR 0004).
The rate limit and the lock for requests that are not idempotent are process-wide (salmon.trackers.base).

Each test runs a local fake tracker that notes when each request arrives, how many requests that are
not idempotent it is answering at once, and how many connections are open. Most tests shorten the
rate limit's period to one second, which changes the times only, not the rule.
"""

import asyncio
import gc
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import asyncclick as click
import pytest
from aiohttp import web
from tenacity import wait_none

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon import cfg
from salmon.trackers import account, base
from salmon.trackers.base import BaseGazelleApi, HttpResponse, SharedLimiter

# How long the fake tracker takes to answer a "slow" request, a "held" one and a POST.
SLOW = 0.6
HELD = 1.0
POST_TIME = 0.3


class FakeApi(BaseGazelleApi):
    cookie = "a-cookie"

    def __init__(self, base_url: str, site_code: str = "RED") -> None:
        self.base_url = base_url
        self.site_code = site_code
        self.site_string = site_code
        super().__init__()
        self._authenticated = True


class FakeTracker:
    """A local tracker noting each request it gets, when, and the connections open at the time."""

    def __init__(self) -> None:
        # (arrival time, site code the client sent, method, action) of each request.
        self.hits: list[tuple[float, str, str, str]] = []
        self.transports: list[asyncio.BaseTransport] = []
        # Connections open when a request arrives, at most; requests that are not idempotent answered at once.
        self.peak_connections = 0
        self.posts = 0
        self.peak_posts = 0
        # Answered 429 once each, with this Retry-After.
        self.limit_once: dict[str, str] = {}
        self.limited = asyncio.Event()

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("*", "/ajax.php", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    def times(self, site: str | None = None) -> list[float]:
        return [at for at, sent_by, _, _ in self.hits if site in (None, sent_by)]

    async def handle(self, request: web.Request) -> web.Response:
        await request.read()
        action = request.query.get("action", "")
        self.hits.append((time.monotonic(), request.headers["X-Site"], request.method, action))
        assert request.transport is not None
        if request.transport not in self.transports:
            self.transports.append(request.transport)
        self.peak_connections = max(
            self.peak_connections, sum(not transport.is_closing() for transport in self.transports)
        )
        if retry_after := self.limit_once.pop(action, None):
            self.limited.set()
            return web.json_response(
                {"status": "failure", "error": "Rate limit exceeded"}, status=429, headers={"Retry-After": retry_after}
            )
        if request.method == "POST":
            self.posts += 1
            self.peak_posts = max(self.peak_posts, self.posts)
            try:
                await asyncio.sleep(POST_TIME)
            finally:
                self.posts -= 1
        elif action in ("slow", "held"):
            await asyncio.sleep(SLOW if action == "slow" else HELD)
        return web.json_response({"status": "success", "response": {}})


def _get(api: FakeApi, action: str = "browse") -> Awaitable[HttpResponse]:
    return api._request("GET", api.base_url + "/ajax.php", params={"action": action})


def _post(api: FakeApi) -> Awaitable[HttpResponse]:
    return api._request("POST", api.base_url + "/ajax.php", params={"action": "upload"}, data={"file": "x"})


def _clients(base_url: str, *site_codes: str, unlimited: bool = False) -> list[FakeApi]:
    """Clients of the fake, one per site code given.

    Args:
        unlimited: Give them one rate limiter no test fills, for the tests about the lock and the pool.
    """
    clients = [FakeApi(base_url, site_code) for site_code in site_codes]
    for client in clients:
        # Lets the fake tell the trackers apart, as they all talk to it.
        client.headers["X-Site"] = client.site_code
        if unlimited:
            # The process-wide limiters are emptied for each test (conftest).
            base._limiters[client.site_code] = SharedLimiter(1000, 1)
    return clients


def _most_within(times: list[float], period: float) -> int:
    """The most requests that arrived within any `period` seconds."""
    times = sorted(times)
    return max((sum(1 for later in times[i:] if later - at < period) for i, at in enumerate(times)), default=0)


def _run(body: Callable[[FakeTracker], Awaitable[None]]) -> FakeTracker:
    """Run a test against a fresh fake tracker on a loop of its own, failing on any session left open."""
    unclosed: list[str] = []
    tracker = FakeTracker()

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        try:
            await body(tracker)
        finally:
            await tracker.runner.cleanup()
        # An unclosed session or connector is only reported once it is collected.
        gc.collect()
        await anyio.sleep(0.05)

    anyio.run(main)
    assert unclosed == []
    return tracker


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


def test_clients_of_one_tracker_open_one_pool_and_one_connection_for_a_post() -> None:
    async def body(tracker: FakeTracker) -> None:
        clients = _clients(await tracker.start(), "RED", "RED", "RED", unlimited=True)
        try:
            await asyncio.gather(*(_get(client, "slow") for client in clients for _ in range(3)))
            await asyncio.gather(
                *(_get(client, "slow") for client in clients for _ in range(3)),
                *(_post(client) for client in clients),
            )
        finally:
            for client in clients:
                await client.close()

    tracker = _run(body)
    assert len(tracker.hits) == 9 + 9 + 3
    # The pool's two connections, and one for the request that is not idempotent in flight.
    assert tracker.peak_connections <= account.POOL_CONNECTIONS + 1
    # The GETs of all three clients went through the same two connections; each POST on one of its own.
    assert len(tracker.transports) == account.POOL_CONNECTIONS + 3


def test_each_event_loop_has_its_own_pool() -> None:
    # One client, used on one loop and then on another, as two anyio.run in one process do.
    api: FakeApi | None = None
    pools: list[object] = []

    async def first(tracker: FakeTracker) -> None:
        nonlocal api
        (api,) = _clients(await tracker.start(), "RED", unlimited=True)
        try:
            await _get(api)
            pools.append(api._session)
        finally:
            await api.close()

    async def second(tracker: FakeTracker) -> None:
        assert api is not None
        api.base_url = await tracker.start()
        try:
            await _get(api)
            pools.append(api._session)
        finally:
            await api.close()

    _run(first)
    _run(second)
    assert len(pools) == 2
    assert pools[0] is not pools[1]


@pytest.mark.usefixtures("short_period")
def test_loops_in_two_threads_do_not_share_state() -> None:
    errors: list[BaseException] = []
    trackers: list[FakeTracker] = []

    async def body(tracker: FakeTracker) -> None:
        trackers.append(tracker)
        (api,) = _clients(await tracker.start(), "RED")
        try:
            await asyncio.gather(*(_get(api) for _ in range(7)), _post(api))
        finally:
            await api.close()

    def thread() -> None:
        try:
            _run(body)
        except BaseException as err:
            errors.append(err)

    threads = [threading.Thread(target=thread) for _ in range(2)]
    for each in threads:
        each.start()
    for each in threads:
        each.join(timeout=30)
    assert errors == []
    assert [len(tracker.hits) for tracker in trackers] == [8, 8]


def test_closing_one_client_leaves_the_pool_to_the_others() -> None:
    async def body(tracker: FakeTracker) -> None:
        first, second = _clients(await tracker.start(), "RED", "RED", unlimited=True)
        await asyncio.gather(_get(first), _get(second))
        pool = first._session
        assert pool is not None
        assert second._session is pool
        await first.close()
        assert not pool.closed
        # The other client goes on on the same connections.
        await _get(second)
        await second.close()
        assert pool.closed
        # A client closed earlier opens a pool again when used again.
        await _get(first)
        assert first._session is not None
        assert first._session is not pool
        await first.close()

    tracker = _run(body)
    assert len(tracker.hits) == 4


def test_a_command_closes_the_pool_its_clients_share() -> None:
    pools = []

    @click.command()
    async def command() -> None:
        tracker = FakeTracker()
        first, second = _clients(await tracker.start(), "RED", "RED", unlimited=True)
        try:
            await asyncio.gather(_get(first), _get(second))
            pools.append(first._session)
        finally:
            await tracker.runner.cleanup()

    command(args=[], standalone_mode=False)
    assert len(pools) == 1
    assert pools[0] is not None
    assert pools[0].closed
