"""One tracker account sends one request that is not idempotent at a time, from whichever thread and event loop."""

import asyncio
import threading
from collections.abc import Callable, Iterator

import anyio
import pytest
from aiohttp import web

from salmon.errors import RequestFailedError
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi, SharedLimiter


class FakeApi(BaseGazelleApi):
    site_code = "LOCKTEST"
    site_string = "Fake"
    tracker_url = "https://announce.fake.example"
    cookie = "fake-cookie"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        super().__init__()
        self._authenticated = True


@pytest.fixture(autouse=True)
def _fresh_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_tracker_limiter", lambda _code: SharedLimiter(100, 1))
    monkeypatch.setattr(base, "_account_locks", {})


class Tracker:
    """A fake tracker on its own thread and loop, counting the requests it is handling at once."""

    def __init__(self, fail_first: bool = False) -> None:
        self.peak = 0
        self.handled = 0
        self._in_flight = 0
        self._count = threading.Lock()
        self._fail_first = fail_first

    async def handle(self, _request: web.Request) -> web.Response:
        with self._count:
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
            self.handled += 1
            fail = self._fail_first and self.handled == 1
        await asyncio.sleep(0.3)
        with self._count:
            self._in_flight -= 1
        if fail:
            return web.json_response({"status": "failure", "error": "no"}, status=400)
        return web.json_response({"status": "success", "response": {}})


@pytest.fixture
def serve() -> Iterator[Callable[[Tracker], str]]:
    stops: list[Callable[[], None]] = []

    def start(tracker: Tracker) -> str:
        loop = asyncio.new_event_loop()
        started = threading.Event()
        address: list[str] = []

        def run() -> None:
            app = web.Application()
            app.router.add_route("*", "/{tail:.*}", tracker.handle)
            runner = web.AppRunner(app, access_log=None)
            loop.run_until_complete(runner.setup())
            loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", 0).start())
            address.append(f"http://127.0.0.1:{runner.addresses[0][1]}")
            started.set()
            loop.run_forever()
            loop.run_until_complete(runner.cleanup())

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert started.wait(5)

        def stop() -> None:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)

        stops.append(stop)
        return address[0]

    yield start
    for stop in stops:
        stop()


def _send(url: str, method: str) -> None:
    async def request() -> None:
        api = FakeApi(url)
        try:
            with anyio.fail_after(10):
                await api._request(
                    method, f"{url}/ajax.php", params={"action": "x"}, data={"a": "b"}, needs_authkey=False
                )
        finally:
            await api.close()

    anyio.run(request)


def _from_two_threads(url: str, method: str) -> list[BaseException]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            _send(url, method)
        except BaseException as error:  # noqa: BLE001 - reported by the test
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    return errors


def test_two_jobs_uploads_to_one_account_never_overlap(serve) -> None:
    tracker = Tracker()

    errors = _from_two_threads(serve(tracker), "POST")

    assert errors == []
    assert tracker.handled == 2
    assert tracker.peak == 1


def test_reads_are_not_held_back_by_the_lock(serve) -> None:
    tracker = Tracker()

    errors = _from_two_threads(serve(tracker), "GET")

    assert errors == []
    assert tracker.peak == 2


def test_a_failed_upload_lets_the_next_one_go(serve) -> None:
    tracker = Tracker(fail_first=True)
    url = serve(tracker)

    with pytest.raises(RequestFailedError):
        _send(url, "POST")
    _send(url, "POST")

    assert tracker.handled == 2


def test_the_rate_window_waits_past_the_trackers_ten_seconds_for_a_sixth_request(monkeypatch) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(base, "_limiters", {})
    limiter = base._tracker_limiter("MARGIN")

    waits = [limiter._wait() for _ in range(6)]

    assert waits[:5] == [0.0] * 5
    assert waits[5] > 10.0
