"""One tracker account sends one upload or edit at a time, from whichever thread and event loop."""

import asyncio
import threading
from collections.abc import Callable, Iterator

import anyio
import pytest
from aiohttp import web

from salmon.errors import RequestFailedError
from salmon.trackers import account
from salmon.trackers.account import SlidingWindowLimiter
from salmon.trackers.base import BaseGazelleApi


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
    monkeypatch.setattr(BaseGazelleApi, "_rate_limiter", SlidingWindowLimiter(100, 1))


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


class EditTracker:
    """A fake tracker's torrent edit form, read slowly enough for two jobs' edits to interleave."""

    def __init__(self) -> None:
        self.description = "Old"

    async def handle(self, request: web.Request) -> web.Response:
        if request.method == "GET":
            shown = self.description
            await asyncio.sleep(0.3)
            return web.Response(text=_edit_page(shown), content_type="text/html")
        self.description = str((await request.post())["release_desc"])
        return web.Response(text="<html><body><h2>Edit successful</h2></body></html>", content_type="text/html")


def _edit_page(description: str) -> str:
    return (
        '<html><body><form name="torrent" method="post"><input type="hidden" name="action" value="takeedit">'
        '<input type="hidden" name="auth" value="AK"><input type="hidden" name="torrentid" value="42">'
        f'<textarea name="release_desc">{description}</textarea></form></body></html>'
    )


@pytest.fixture
def serve() -> Iterator[Callable[[Tracker | EditTracker], str]]:
    stops: list[Callable[[], None]] = []

    def start(tracker: Tracker | EditTracker) -> str:
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
        is_up = started.wait(5)
        assert is_up

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


def _append(url: str, addition: str) -> None:
    async def edit() -> None:
        api = FakeApi(url)
        try:
            with anyio.fail_after(10):
                await api.append_to_torrent_description(42, addition)
        finally:
            await api.close()

    anyio.run(edit)


def _at_once(*jobs: Callable[[], None]) -> list[BaseException]:
    errors: list[BaseException] = []

    def run(job: Callable[[], None]) -> None:
        try:
            job()
        except BaseException as error:  # noqa: BLE001 - reported by the test
            errors.append(error)

    threads = [threading.Thread(target=run, args=(job,)) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    return errors


@pytest.fixture
def request_loop() -> Iterator[None]:
    """salmon web's request loop, in a thread of its own: each job's tracker requests are handed over to it."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    stop: list[asyncio.Event] = []

    async def main() -> None:
        stop.append(asyncio.Event())
        async with account.requests_on_this_loop():
            ready.set()
            await stop[0].wait()

    thread = threading.Thread(target=loop.run_until_complete, args=(main(),))
    thread.start()
    is_up = ready.wait(5)
    assert is_up
    yield
    loop.call_soon_threadsafe(stop[0].set)
    thread.join(10)
    loop.close()


@pytest.mark.usefixtures("request_loop")
def test_two_jobs_uploads_to_one_account_never_overlap(serve) -> None:
    tracker = Tracker()
    url = serve(tracker)

    errors = _at_once(lambda: _send(url, "POST"), lambda: _send(url, "POST"))

    assert errors == []
    assert tracker.handled == 2
    assert tracker.peak == 1


def test_reads_are_not_held_back_by_the_lock(serve) -> None:
    tracker = Tracker()
    url = serve(tracker)

    errors = _at_once(lambda: _send(url, "GET"), lambda: _send(url, "GET"))

    assert errors == []
    assert tracker.peak == 2


@pytest.mark.usefixtures("request_loop")
def test_two_jobs_editing_one_description_keep_both_additions(serve) -> None:
    tracker = EditTracker()
    url = serve(tracker)

    errors = _at_once(lambda: _append(url, "A: "), lambda: _append(url, "B: "))

    assert errors == []
    assert tracker.description in ("A: B: Old", "B: A: Old")


def test_a_failed_upload_lets_the_next_one_go(serve) -> None:
    tracker = Tracker(fail_first=True)
    url = serve(tracker)

    with pytest.raises(RequestFailedError):
        _send(url, "POST")
    _send(url, "POST")

    assert tracker.handled == 2


def test_the_rate_window_waits_past_the_trackers_ten_seconds_for_a_sixth_request() -> None:
    async def wait_for_a_sixth() -> float:
        limiter = account.TrackerAccount().limiter
        for _ in range(5):
            async with limiter:
                pass
        return limiter._wait()

    assert anyio.run(wait_for_a_sixth) > 10.0
