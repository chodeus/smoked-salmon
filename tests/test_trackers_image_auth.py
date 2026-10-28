"""RED's image host credentials: one imgauth call kept until near expiry, sent only on an image's query, never shown."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from tenacity import wait_none

from salmon import cfg
from salmon.errors import RequestError, RequestFailedError
from salmon.trackers import base
from salmon.trackers.account import SlidingWindowLimiter
from salmon.trackers.base import BaseGazelleApi

KEY = "Im4geH0stK3yMarker"
EXPIRY = "4102444800"
USER = "4242424"
AUTH = {"h": KEY, "e": EXPIRY, "u": USER}
IMAGE = b"\xff\xd8\xff\xe0 an image"


class FakeRed(BaseGazelleApi):
    site_code = "FAKERED"
    site_string = "FakeRED"
    tracker_url = "https://announce.fake.example"
    SIGNED_IMAGES = True

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.cookie = "a-session-cookie"
        super().__init__()
        self.api_key = "an-api-key"
        self._authenticated = True


class Tracker:
    """A fake RED: imgauth answers `answer`, /i/ serves an image only on AUTH's query, any other GET answers `page`."""

    def __init__(self) -> None:
        self.answer: Any = {"status": "success", "response": {"h": KEY, "e": int(EXPIRY), "u": int(USER)}}
        self.page: Any = {"status": "success", "response": {}}
        self.headers: dict[str, str] = {}
        self.seen: list[web.Request] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.seen.append(request)
        if request.path.startswith("/i/"):
            if dict(request.query) != AUTH:
                return web.Response(status=403, text="403 Forbidden")
            return web.Response(body=IMAGE, content_type="image/jpeg")
        if request.query.get("action") == "imgauth":
            return web.json_response(self.answer)
        return web.json_response(self.page, headers=self.headers)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BaseGazelleApi, "_rate_limiter", SlidingWindowLimiter(100, 1))
    monkeypatch.setattr(base, "_learned_secrets", set())
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]


@pytest.fixture
async def red() -> AsyncIterator[Callable[..., Awaitable[tuple[FakeRed, Tracker]]]]:
    runners: list[web.AppRunner] = []
    apis: list[FakeRed] = []

    async def start(api_class: type[FakeRed] = FakeRed) -> tuple[FakeRed, Tracker]:
        tracker = Tracker()
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", tracker.handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        runners.append(runner)
        api = api_class(f"http://127.0.0.1:{runner.addresses[0][1]}")
        apis.append(api)
        return api, tracker

    yield start
    for api in apis:
        await api.close()
    for runner in runners:
        await runner.cleanup()


async def _fetch_image(api: FakeRed, auth: dict[str, str]) -> base.HttpResponse:
    return await api._request(
        "GET", f"{api.base_url}/i/cover.jpg", params=auth, prefer_api_key=True, needs_authkey=False, binary=True
    )


async def test_one_imgauth_call_with_the_api_key_alone_is_kept_until_a_minute_before_it_expires(red) -> None:
    api, tracker = await red()

    first = await api.image_auth()
    second = await api.image_auth()

    assert first == second == AUTH
    [call] = tracker.seen
    assert call.query["action"] == "imgauth"
    assert (call.headers.get("Authorization"), call.headers.get("Cookie")) == ("an-api-key", None)
    api._image_auth["e"] = str(int(time.time()) + 30)
    await api.image_auth()
    assert len(tracker.seen) == 2


async def test_without_an_api_key_the_session_cookie_gets_them(red) -> None:
    api, tracker = await red()
    api.api_key = ""

    auth = await api.image_auth()

    assert auth == AUTH
    [call] = tracker.seen
    assert "Authorization" not in call.headers
    assert "session=" in call.headers["Cookie"]


async def test_a_tracker_that_signs_no_images_is_never_asked(red) -> None:
    class Unsigned(FakeRed):
        SIGNED_IMAGES = False

    api, tracker = await red(Unsigned)

    auth = await api.image_auth()

    assert auth == {}
    assert tracker.seen == []


async def test_only_the_key_becomes_a_secret(red) -> None:
    api, _ = await red()

    await api.image_auth()

    assert KEY in api._secrets()
    assert base.learned_secrets() == [KEY]


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "failure", "error": "bad action"},
        {"status": "success", "response": {"h": KEY, "e": int(EXPIRY)}},
        {"status": "success", "response": {"h": "", "e": int(EXPIRY), "u": int(USER)}},
        {"status": "success", "response": {"h": "k3y4567", "e": int(EXPIRY), "u": int(USER)}},
        {"status": "success", "response": {"h": KEY, "e": "soon", "u": int(USER)}},
        {"status": "success", "response": []},
        ["not", "an", "object"],
    ],
    ids=[
        "refused",
        "no user id",
        "empty key",
        "key too short to mask",
        "expiry not a number",
        "response not an object",
        "not an object",
    ],
)
async def test_an_answer_without_all_three_is_refused_and_nothing_is_kept(red, answer) -> None:
    api, tracker = await red()
    tracker.answer = answer

    with pytest.raises(RequestFailedError, match="FakeRED"):
        await api.image_auth()

    assert api._image_auth == {}
    assert base.learned_secrets() == []


async def test_a_signed_fetch_gets_the_image_and_keeps_no_signed_url(red) -> None:
    api, tracker = await red()
    auth = await api.image_auth()

    response = await _fetch_image(api, auth)

    assert response.content == IMAGE
    assert dict(tracker.seen[-1].query) == AUTH
    assert response.url == f"{api.base_url}/i/cover.jpg"


async def test_debug_output_shows_neither_the_imgauth_answer_nor_a_signed_fetchs_query(
    red, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cfg.tracker, "debug_connection", True)
    api, _ = await red()

    auth = await api.image_auth()
    response = await _fetch_image(api, auth)

    printed = capsys.readouterr()
    assert response.content == IMAGE
    assert "<the image host's credentials>" in printed.out
    assert '[DEBUG] params: {"h":"[REDACTED]","e":"[REDACTED]","u":"[REDACTED]"}' in printed.out
    assert [value for value in AUTH.values() if value in printed.out + printed.err] == []


async def test_debug_output_masks_the_query_of_every_signed_image_url_in_an_answer(red, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg.tracker, "debug_connection", True)
    api, tracker = await red()
    signed = f"/i/cover.jpg?h={KEY}&e={EXPIRY}&u={USER}"
    tracker.page = {
        "status": "success",
        "response": {"wikiImage": f"{api.base_url}{signed}", "wikiBody": f'<img src="{signed.replace("&", "&amp;")}">'},
    }

    await api._request("GET", f"{api.base_url}/ajax.php", params={"action": "torrentgroup"}, needs_authkey=False)

    printed = capsys.readouterr()
    assert "/i/cover.jpg?h=[REDACTED]&e=[REDACTED]&u=[REDACTED]" in printed.out
    assert "/i/cover.jpg?h=[REDACTED]&amp;e=[REDACTED]&amp;u=[REDACTED]" in printed.out
    assert [value for value in AUTH.values() if value in printed.out + printed.err] == []


async def test_a_network_error_naming_the_signed_url_shows_none_of_it(red, monkeypatch, capsys) -> None:
    api, _ = await red()
    auth = await api.image_auth()

    def cut_off(_session: aiohttp.ClientSession, method: str, url: str, **kwargs: Any) -> Any:
        query = "&".join(f"{name}={value}" for name, value in kwargs["params"].items())
        raise aiohttp.ClientConnectionError(f"Connection lost on {method} {url}?{query}")

    monkeypatch.setattr(aiohttp.ClientSession, "request", cut_off)

    with pytest.raises(RequestError) as caught:
        await _fetch_image(api, auth)

    printed = capsys.readouterr()
    shown = f"{caught.value}{printed.out}{printed.err}"
    assert "Connection lost on GET" in shown
    assert [value for value in AUTH.values() if value in shown] == []


async def test_debug_output_masks_a_known_secret_a_response_header_repeats(red, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg.tracker, "debug_connection", True)
    api, tracker = await red()
    tracker.headers = {"X-Echo": f"request from {api.api_key}"}

    await api._request("GET", f"{api.base_url}/ajax.php", params={"action": "index"}, needs_authkey=False)

    printed = capsys.readouterr()
    assert '"X-Echo":"request from [REDACTED]"' in printed.out
    assert "an-api-key" not in printed.out + printed.err


async def test_debug_output_masks_a_signed_image_link_in_a_response_header(red, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg.tracker, "debug_connection", True)
    api, tracker = await red()
    tracker.headers = {"Link": f"<{api.base_url}/i/1.jpg?h={KEY}&e={EXPIRY}&u={USER}>; rel=preload"}

    await api._request("GET", f"{api.base_url}/ajax.php", params={"action": "index"}, needs_authkey=False)

    printed = capsys.readouterr()
    assert "/i/1.jpg?h=[REDACTED]&e=[REDACTED]&u=[REDACTED]>; rel=preload" in printed.out
    assert [value for value in AUTH.values() if value in printed.out + printed.err] == []
