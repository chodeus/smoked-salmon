"""Tracker redirects and the site log, against real local servers."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from aiohttp import web
from aiolimiter import AsyncLimiter

import salmon.cross_upload as cross_upload
from salmon import cfg
from salmon.errors import ImageUploadFailed, LoginError, RequestFailedError
from salmon.images import red as red_image_host
from salmon.trackers.base import BaseGazelleApi, _tracker_limiter


class CountingLimiter(AsyncLimiter):
    """A limiter loose enough not to slow the tests, which counts the slots taken."""

    def __init__(self, *_args) -> None:
        super().__init__(100, 1)
        self.slots = 0

    async def acquire(self, amount: float = 1) -> None:
        self.slots += 1
        await super().acquire(amount)


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    tracker_url = "https://announce.fake.example"

    def __init__(self, base_url: str, cookie: str = "a-cookie") -> None:
        self.base_url = base_url
        self.cookie = cookie
        super().__init__()
        self._authenticated = True


@pytest.fixture(autouse=True)
def _counting_limiter(monkeypatch):
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", False)
    monkeypatch.setattr("salmon.trackers.base.AsyncLimiter", CountingLimiter)


@pytest.fixture
async def serve():
    runners: list[web.AppRunner] = []

    async def _serve(**handlers) -> str:
        app = web.Application()
        for path, handler in handlers.items():
            app.router.add_route("*", f"/{path}.php", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        runners.append(runner)
        return f"http://127.0.0.1:{runner.addresses[0][1]}"

    yield _serve
    for runner in runners:
        await runner.cleanup()


@pytest.fixture
async def api_for():
    made: list[FakeApi] = []

    def _make(base_url: str, cookie: str = "a-cookie") -> FakeApi:
        made.append(FakeApi(base_url, cookie))
        return made[-1]

    yield _make
    for api in made:
        await api.close()


async def test_a_login_redirect_stops_before_the_login_page(serve, api_for):
    hits: list[str] = []

    async def log(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPFound("/login.php")

    async def login(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPFound("/log.php")

    url = await serve(log=log, login=login)
    with pytest.raises(LoginError):
        await api_for(url)._request("GET", f"{url}/log.php", params={"page": 1})
    assert hits == ["/log.php"]


async def test_each_redirect_hop_takes_a_rate_limiter_slot(serve, api_for):
    hits: list[str] = []

    async def torrents(request: web.Request) -> web.Response:
        hits.append(request.path_qs)
        if "id" not in request.query:
            raise web.HTTPFound(f"torrents.php?id=2&torrentid={int(request.query['torrentid'])}#torrent1")
        return web.Response(text="group page")

    url = await serve(torrents=torrents)
    api = api_for(url)
    group_id = await api.get_redirect_torrentgroupid(1)
    assert group_id == 2
    assert hits == ["/torrents.php?torrentid=1", "/torrents.php?id=2&torrentid=1"]
    assert cast("CountingLimiter", _tracker_limiter("RED")).slots == len(hits)


async def test_a_redirected_post_is_fetched_with_get(serve, api_for):
    hits: list[tuple[str, str]] = []

    async def upload(request: web.Request) -> web.Response:
        hits.append((request.method, request.path))
        form = await request.post()
        assert form["auth"] == "an-authkey"
        raise web.HTTPFound("/torrents.php?id=5")

    async def torrents(request: web.Request) -> web.Response:
        hits.append((request.method, request.path))
        return web.Response(text="group page")

    url = await serve(upload=upload, torrents=torrents)
    resp = await api_for(url)._request("POST", f"{url}/upload.php", data={"auth": "an-authkey"})
    assert hits == [("POST", "/upload.php"), ("GET", "/torrents.php")]
    assert resp.url == f"{url}/torrents.php?id=5"
    assert resp.text == "group page"


async def test_an_upload_filling_a_request_follows_both_hops(serve, api_for):
    hits: list[tuple[str, str]] = []

    async def upload(request: web.Request) -> web.Response:
        hits.append((request.method, request.path_qs))
        raise web.HTTPFound("/requests.php?action=takefill&requestid=7")

    async def requests(request: web.Request) -> web.Response:
        hits.append((request.method, request.path_qs))
        if request.query["action"] == "takefill":
            raise web.HTTPFound("/requests.php?action=view&id=7")
        return web.Response(text="request page")

    url = await serve(upload=upload, requests=requests)
    resp = await api_for(url)._request("POST", f"{url}/upload.php", data={"auth": "an-authkey"})
    assert resp.url == f"{url}/requests.php?action=view&id=7"
    assert hits == [
        ("POST", "/upload.php"),
        ("GET", "/requests.php?action=takefill&requestid=7"),
        ("GET", "/requests.php?action=view&id=7"),
    ]


async def test_a_redirect_to_another_site_is_not_requested(serve, api_for):
    other_hits: list[str | None] = []

    async def elsewhere(request: web.Request) -> web.Response:
        other_hits.append(request.headers.get("Cookie"))
        return web.Response(text="not the tracker")

    other = await serve(torrents=elsewhere)

    async def torrents(_request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{other}/torrents.php")

    url = await serve(torrents=torrents)
    with pytest.raises(RequestFailedError):
        await api_for(url)._request("GET", f"{url}/torrents.php")
    assert other_hits == []


async def test_a_redirect_loop_is_cut_short(serve, api_for):
    hits: list[str] = []

    async def ping(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPFound("/pong.php")

    async def pong(request: web.Request) -> web.Response:
        hits.append(request.path)
        raise web.HTTPFound("/ping.php")

    url = await serve(ping=ping, pong=pong)
    with pytest.raises(RequestFailedError):
        await api_for(url)._request("GET", f"{url}/ping.php")
    assert hits == ["/ping.php", "/pong.php", "/ping.php", "/pong.php"]


async def test_a_raw_site_fetch_takes_a_slot_and_stays_put(serve, api_for):
    seen: list[str | None] = []

    async def image(request: web.Request) -> web.Response:
        seen.append(request.cookies.get("session"))
        raise web.HTTPFound("https://elsewhere.example/steal")

    url = await serve(image=image)
    async with api_for(url).site_get(f"{url}/image.php") as resp:
        assert resp.status == 302
    assert seen == ["a-cookie"]
    assert cast("CountingLimiter", _tracker_limiter("RED")).slots == 1


async def _stream_image(request: web.Request, chunks: int, size: int, pause: float) -> web.StreamResponse:
    resp = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
    await resp.prepare(request)
    for _ in range(chunks):
        await resp.write(b"x" * size)
        await asyncio.sleep(pause)
    await resp.write_eof()
    return resp


async def test_a_trickled_raw_site_fetch_still_times_out(serve, api_for):
    async def image(request: web.Request) -> web.StreamResponse:
        return await _stream_image(request, chunks=10, size=1, pause=0.3)

    url = await serve(image=image)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(TimeoutError):
        async with api_for(url).site_get(f"{url}/image.php", timeout_secs=1) as resp:
            await resp.read()
    assert loop.time() - start < 2


async def test_a_rehosted_red_image_is_read_to_the_end(serve, api_for, monkeypatch):
    async def image(request: web.Request) -> web.StreamResponse:
        return await _stream_image(request, chunks=3, size=65536, pause=0.05)

    uploaded: list[int] = []

    async def upload_file(path: str) -> tuple[str, None]:
        uploaded.append(len(Path(path).read_bytes()))
        return "https://files.catbox.moe/abc.jpg", None

    host = SimpleNamespace(ImageUploader=lambda: SimpleNamespace(upload_file=upload_file))
    monkeypatch.setitem(cross_upload.HOSTS, "catbox", host)
    url = await serve(image=image)
    await cross_upload._rehost_red_image(f"{url}/image.php", api_for(url), "catbox")
    assert uploaded == [3 * 65536]


async def test_clients_of_one_tracker_share_one_budget(api_for):
    first, second = api_for("http://127.0.0.1:1"), api_for("http://127.0.0.1:1")
    assert first._rate_limiter is second._rate_limiter


class FakeLog:
    """A tracker whose log.php needs the session cookie "good", else bounces to login.php."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.requested_when_page_one_answered: list[str] = []

    async def log(self, request: web.Request) -> web.Response:
        self.hits.append(request.path_qs)
        if request.cookies.get("session") != "good":
            raise web.HTTPFound("/login.php")
        page = int(request.query["page"])
        if page == 1:
            # Slow enough for pages requested alongside page 1 to arrive before it is answered.
            await asyncio.sleep(0.2)
            self.requested_when_page_one_answered = list(self.hits)
        return web.Response(
            text=f'<span class="log_upload"><a href="torrents.php?torrentid={page}">{page}</a>'
            f" (Artist - Title {page}) (x)</span>",
            content_type="text/html",
        )

    async def login(self, request: web.Request) -> web.Response:
        self.hits.append(request.path)
        raise web.HTTPFound("/log.php")


async def test_no_session_cookie_skips_the_site_log(serve, api_for, capsys):
    tracker = FakeLog()
    url = await serve(log=tracker.log, login=tracker.login)

    uploads = await api_for(url, cookie="").get_uploads_from_log()
    out = capsys.readouterr().out
    assert uploads == []
    assert tracker.hits == []
    assert "needs a session cookie" in out


async def test_an_expired_cookie_costs_one_request(serve, api_for):
    tracker = FakeLog()
    url = await serve(log=tracker.log, login=tracker.login)

    uploads = await api_for(url, cookie="expired").get_uploads_from_log()
    assert uploads == []
    assert tracker.hits == ["/log.php?page=1"]


async def test_a_valid_cookie_reads_every_page_after_the_first(serve, api_for):
    tracker = FakeLog()
    url = await serve(log=tracker.log, login=tracker.login)

    uploads = await api_for(url, cookie="good").get_uploads_from_log()
    assert tracker.requested_when_page_one_answered == ["/log.php?page=1"]
    assert sorted(tracker.hits) == sorted(f"/log.php?page={page}" for page in range(1, 11))
    assert sorted(uploads) == sorted((str(page), "Artist", f"Title {page}") for page in range(1, 11))


_SECRETS = ("SYNTH-API-KEY", "SYNTH-SESSION-COOKIE", "SYNTH-AUTHKEY")


def _red_image_host_against(monkeypatch, url: str, api_key: str, authenticated: bool = True) -> None:
    def make_site() -> FakeApi:
        site = FakeApi(url, cookie="SYNTH-SESSION-COOKIE")
        site.api_key = api_key
        site.authkey = "SYNTH-AUTHKEY"
        site._authenticated = authenticated
        return site

    monkeypatch.setattr(red_image_host, "RedApi", make_site)
    monkeypatch.setattr(
        red_image_host, "cfg", SimpleNamespace(tracker=SimpleNamespace(red=SimpleNamespace(session="x")))
    )


async def test_the_red_image_host_sends_only_the_api_key_when_one_is_set(serve, monkeypatch, tmp_path):
    seen: list[dict] = []

    async def ajax(request: web.Request) -> web.Response:
        form = await request.post()
        seen.append({"query": dict(request.query), "headers": dict(request.headers), "form": set(form)})
        return web.json_response({"status": "success", "response": {"url": "https://redacted.sh/i/x.png"}})

    url = await serve(ajax=ajax)
    # A fresh client: an index call for the authkey would show up as a second request.
    _red_image_host_against(monkeypatch, url, "SYNTH-API-KEY", authenticated=False)
    image = tmp_path / "cover.png"
    image.write_bytes(b"png-data")

    assert (await red_image_host.ImageUploader().upload_file(str(image)))[0] == "https://redacted.sh/i/x.png"
    [request] = seen
    assert request["query"] == {"action": "upload_image"}
    assert request["headers"]["Authorization"] == "SYNTH-API-KEY"
    assert "Cookie" not in request["headers"]
    assert request["form"] == {"file"}


@pytest.mark.parametrize("api_key", ["SYNTH-API-KEY", ""], ids=["api key", "session cookie"])
async def test_the_red_image_host_never_repeats_a_credential(serve, monkeypatch, tmp_path, capsys, api_key):
    # RED's answer is untrusted: here it echoes back every credential it was sent.
    async def ajax(request: web.Request) -> web.Response:
        form = await request.post()
        echo = f"{request.headers.get('Authorization')} {request.headers.get('Cookie')} {form.get('auth')}"
        return web.json_response({"status": "failure", "error": f"rejected: {echo}"}, status=400)

    url = await serve(ajax=ajax)
    _red_image_host_against(monkeypatch, url, api_key)
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", True)
    image = tmp_path / "cover.png"
    image.write_bytes(b"png-data")

    with pytest.raises(ImageUploadFailed) as excinfo:
        await red_image_host.ImageUploader().upload_file(str(image))
    shown = str(excinfo.value) + "".join(capsys.readouterr())
    assert "rejected" in shown
    assert [secret for secret in _SECRETS if secret in shown] == []
