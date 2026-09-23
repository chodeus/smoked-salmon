"""The kept-alive tracker connection pool, against real local servers."""

import asyncio
import gc
import weakref
from typing import Any, cast

import asyncclick as click
import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter
from tenacity import wait_fixed

import salmon.trackers
from salmon import cfg
from salmon.checks.connection import check_tracker_connection
from salmon.errors import RequestFailedError, UnknownOutcomeError
from salmon.trackers.base import BaseGazelleApi, _open_pools
from salmon.webui.jobs import JobManager


class FakeApi(BaseGazelleApi):
    site_code = "FAKE"
    site_string = "Fake"
    tracker_url = "https://announce.fake.example"
    cookie = "fake-cookie"

    def __init__(self, base_url: str = "http://127.0.0.1:1") -> None:
        self.base_url = base_url
        super().__init__()
        self._authenticated = True


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", False)
    # Measures connection reuse, not throttling.
    monkeypatch.setattr("salmon.trackers.base.AsyncLimiter", lambda *_a, **_k: AsyncLimiter(100, 1))


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


async def test_gathered_requests_reuse_a_small_pool(serve):
    ports: list[int] = []

    async def handler(request: web.Request) -> web.Response:
        ports.append(_peer_port(request))
        return _ok()

    api = FakeApi(await serve(handler))
    try:
        await asyncio.gather(*(api._request("GET", f"{api.base_url}/ajax.php") for _ in range(6)))
    finally:
        await api.close()
    assert len(ports) == 6
    assert len(set(ports)) <= 2


async def test_queued_requests_do_not_time_out_while_waiting(serve):
    hits: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        hits.append(request.path)
        await asyncio.sleep(0.4)
        return _ok()

    api = FakeApi(await serve(handler))
    try:
        # Six 0.4s answers over two connections take 1.2s, past each request's 1s timeout
        # only if the wait for a free connection counts towards it.
        await asyncio.gather(*(api._request("GET", f"{api.base_url}/ajax.php", timeout_secs=1) for _ in range(6)))
    finally:
        await api.close()
    assert len(hits) == 6


async def test_a_trickled_answer_still_times_out(serve, monkeypatch):
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))

    async def handler(request: web.Request) -> web.StreamResponse:
        await request.read()
        resp = web.StreamResponse()
        await resp.prepare(request)
        # Each byte lands well inside sock_read, so only a bound on the whole read stops it.
        for _ in range(30):
            await resp.write(b" ")
            await asyncio.sleep(0.1)
        return resp

    api = FakeApi(await serve(handler))
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(UnknownOutcomeError):
            await api._request("POST", f"{api.base_url}/upload.php", data={"x": "1"}, timeout_secs=1)
    finally:
        await api.close()
    assert asyncio.get_running_loop().time() - started < 2


async def test_api_key_requests_stay_cookie_free(serve):
    sent: list[str | None] = []

    async def handler(request: web.Request) -> web.Response:
        sent.append(request.headers.get("Cookie"))
        response = _ok()
        response.set_cookie("planted", "by-the-server")
        return response

    base = await serve(handler)
    # A named host: a cookie jar discards cookies set by a bare IP address.
    api = FakeApi(base.replace("127.0.0.1", "localhost"))
    api.api_key = "an-api-key"
    try:
        await api._request("GET", f"{api.base_url}/ajax.php")
        await api._request("GET", f"{api.base_url}/ajax.php", prefer_api_key=True)
    finally:
        await api.close()
    assert sent[0] == "session=fake-cookie"
    assert sent[1] is None


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


async def test_a_web_job_closes_its_tracker_pool(serve):
    base = await serve(_answer_ok)
    sessions: list[Any] = []

    async def factory(_job) -> None:
        api = FakeApi(base)
        await api._request("GET", f"{base}/ajax.php")
        sessions.append(api._session)

    job = JobManager().create_threaded("test", "pool", factory)
    assert job.thread is not None
    await asyncio.to_thread(job.thread.join, 10)

    assert job.status == "done", job.error
    assert sessions and sessions[0].closed


async def test_the_connection_check_closes_its_pool(serve, monkeypatch):
    base = await serve(_answer_ok)
    made: list[FakeApi] = []

    def make() -> FakeApi:
        made.append(FakeApi(base))
        return made[-1]

    monkeypatch.setattr(salmon.trackers, "get_class", lambda _code: make)
    result = await check_tracker_connection("FAKE")

    assert result["session_ok"], result
    assert cast("Any", made[0])._session is None


async def test_a_long_lived_context_keeps_no_tracker_once_its_pool_is_closed(serve):
    # The web UI's server context lives until shutdown; each connection check must not pile up on it.
    base = await serve(_answer_ok)
    async with click.Context(click.Command("web")) as ctx:
        refs = []
        for _ in range(3):
            api = FakeApi(base)
            await api._request("GET", f"{base}/ajax.php")
            await api.close()
            refs.append(weakref.ref(api))
            del api
        gc.collect()
        assert all(ref() is None for ref in refs)
        assert not _open_pools.get(ctx)


async def test_a_pool_left_open_still_closes_with_its_context(serve):
    base = await serve(_answer_ok)
    async with click.Context(click.Command("cli")):
        api = FakeApi(base)
        await api._request("GET", f"{base}/ajax.php")
        session = api._session
        del api
        gc.collect()
    assert session is not None and session.closed


async def test_closing_a_pool_never_walks_every_contexts_pools(serve, monkeypatch):
    # Web UI job threads add their own contexts to the shared registry; walking it here can race them.
    class NoWalking(weakref.WeakKeyDictionary):
        def values(self):
            raise AssertionError("close() walked the shared registry")

        def items(self):
            raise AssertionError("close() walked the shared registry")

    monkeypatch.setattr("salmon.trackers.base._open_pools", NoWalking())
    base = await serve(_answer_ok)
    async with click.Context(click.Command("web")):
        api = FakeApi(base)
        await api._request("GET", f"{base}/ajax.php")
        await api.close()
    assert api._session is None
