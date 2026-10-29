"""What this fork's tracker sessions add to upstream's: a POST's own connection and pools that close."""

import asyncio
import gc
import weakref
from typing import Any, cast

import asyncclick as click
import pytest
from aiohttp import web

import salmon.trackers
from salmon import cfg
from salmon.errors import RequestFailedError
from salmon.trackers import account
from salmon.trackers.account import SlidingWindowLimiter
from salmon.trackers.base import BaseGazelleApi
from salmon.webui import kinds


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "Fake"
    tracker_url = "https://announce.fake.example"
    cookie = "fake-cookie"

    def __init__(self, base_url: str = "http://127.0.0.1:1") -> None:
        self.base_url = base_url
        super().__init__()
        self._authenticated = True


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(cfg.tracker, "debug_connection", False)
    # Measures connection reuse, not throttling.
    monkeypatch.setattr(BaseGazelleApi, "_rate_limiter", SlidingWindowLimiter(100, 1))


@pytest.fixture
async def serve():
    runners: list[web.AppRunner] = []

    async def _serve(handler, method: str = "*") -> str:
        app = web.Application()
        app.router.add_route(method, "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        runners.append(runner)
        return f"http://127.0.0.1:{runner.addresses[0][1]}"

    yield _serve
    for runner in runners:
        await runner.cleanup()


def _ok() -> web.Response:
    return web.json_response({"status": "success", "response": {"authkey": "a", "passkey": "p"}})


async def _answer_ok(_request: web.Request) -> web.Response:
    return _ok()


def _peer_port(request: web.Request) -> int:
    assert request.transport is not None
    return request.transport.get_extra_info("peername")[1]


async def test_a_post_goes_on_a_fresh_connection_and_leaves_the_pool_alone(serve):
    seen: list[tuple[str, int]] = []

    async def handler(request: web.Request) -> web.Response:
        await request.read()
        seen.append((request.method, _peer_port(request)))
        if request.path == "/slow":
            await asyncio.sleep(0.3)
        return _ok()

    api = FakeApi(await serve(handler))
    try:
        await api._request("GET", f"{api.base_url}/ajax.php")
        # An idle pooled connection is free: a pooled POST would take it.
        await api._request("POST", f"{api.base_url}/upload.php", data={"x": "0"})
        # A GET in flight on the pool must survive the POST.
        await asyncio.gather(
            api._request("GET", f"{api.base_url}/slow"),
            api._request("POST", f"{api.base_url}/upload.php", data={"x": "1"}),
        )
        await api._request("GET", f"{api.base_url}/ajax.php")
    finally:
        await api.close()

    post_ports = [port for method, port in seen if method == "POST"]
    get_ports = {port for method, port in seen if method == "GET"}
    assert len(post_ports) == 2
    assert not set(post_ports) & get_ports
    assert seen[0][1] == seen[-1][1]


async def test_a_session_cookie_follows_a_redirect_on_the_tracker_only(serve):
    received: dict[str, str | None] = {}

    async def elsewhere(request: web.Request) -> web.Response:
        received["elsewhere"] = request.headers.get("Cookie")
        return _ok()

    other = await serve(elsewhere)

    async def tracker(request: web.Request) -> web.Response:
        received[request.path] = request.headers.get("Cookie")
        if request.path == "/hop":
            raise web.HTTPFound("/landed")
        if request.path == "/away":
            raise web.HTTPFound(f"{other}/steal")
        return _ok()

    api = FakeApi((await serve(tracker)).replace("127.0.0.1", "localhost"))
    try:
        await api._request("GET", f"{api.base_url}/hop")
        with pytest.raises(RequestFailedError):
            await api._request("GET", f"{api.base_url}/away")
    finally:
        await api.close()
    assert received["/landed"] == "session=fake-cookie"
    assert "elsewhere" not in received


async def test_the_connection_check_closes_its_pool(serve, monkeypatch):
    base = await serve(_answer_ok)
    made: list[FakeApi] = []

    def make() -> FakeApi:
        made.append(FakeApi(base))
        return made[-1]

    monkeypatch.setattr(salmon.trackers, "get_class", lambda _code: make)
    result = await kinds.connection_check(kinds.ConnectionCheckParams(trackers=["FAKE"]))

    assert result["trackers"][0]["cookie"] == "ok", result
    assert cast("Any", made[0])._session is None


async def test_a_long_lived_context_keeps_no_tracker_once_its_pool_is_closed(serve):
    # The web UI's server context lives until shutdown; each connection check must not pile up on it.
    base = await serve(_answer_ok)
    async with account.requests_on_this_loop(), click.Context(click.Command("web")):
        refs = []
        for _ in range(3):
            api = FakeApi(base)
            await api._request("GET", f"{base}/ajax.php")
            await api.close()
            refs.append(weakref.ref(api))
            del api
        gc.collect()
        assert all(ref() is None for ref in refs)


async def test_a_pool_left_open_still_closes_with_its_context(serve):
    base = await serve(_answer_ok)
    async with click.Context(click.Command("cli")):
        api = FakeApi(base)
        await api._request("GET", f"{base}/ajax.php")
        session = api._session
        del api
        gc.collect()
    assert session is not None and session.closed
