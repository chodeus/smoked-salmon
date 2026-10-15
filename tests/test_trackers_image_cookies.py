"""RED's image host cookies: got from one index call with the session cookie, sent only when given, never printed."""

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from aiohttp import web

from salmon import cfg
from salmon.errors import RequestFailedError
from salmon.trackers import base
from salmon.trackers.base import BaseGazelleApi, SharedLimiter

SIGNATURE = "test/sig+nature=0123"
IMGAUTH = {"imgauth_u": "12345", "imgauth_h": SIGNATURE, "imgauth_e": "1700000000"}


class FakeRed(BaseGazelleApi):
    site_code = "FAKERED"
    site_string = "FakeRED"
    tracker_url = "https://announce.fake.example"
    IMAGE_COOKIES = ("imgauth_u", "imgauth_h", "imgauth_e")

    def __init__(self, base_url: str, session: str = "a-session-cookie") -> None:
        self.base_url = base_url
        self.cookie = session
        super().__init__()
        self.api_key = "an-api-key"
        self._authenticated = True


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_tracker_limiter", lambda _code: SharedLimiter(100, 1))
    monkeypatch.setattr(base, "_learned_secrets", set())


@pytest.fixture
async def red() -> AsyncIterator[Callable[..., Awaitable[tuple[FakeRed, list[web.Request]]]]]:
    runners: list[web.AppRunner] = []
    apis: list[FakeRed] = []

    async def start(cookies: dict[str, str] = IMGAUTH, session: str = "a-session-cookie"):
        seen: list[web.Request] = []

        async def handle(request: web.Request) -> web.Response:
            seen.append(request)
            response = web.json_response({"status": "success", "response": {"authkey": "a", "passkey": "p"}})
            for name, value in cookies.items():
                # Raw: aiohttp's set_cookie would quote a value holding / + =.
                response.headers.add("Set-Cookie", f"{name}={value}; path=/i/; HttpOnly")
            return response

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        runners.append(runner)
        api = FakeRed(f"http://127.0.0.1:{runner.addresses[0][1]}", session)
        apis.append(api)
        return api, seen

    yield start
    for api in apis:
        await api.close()
    for runner in runners:
        await runner.cleanup()


async def test_the_cookies_come_from_one_index_call_with_the_session_cookie(red) -> None:
    api, seen = await red()

    cookies = await api.image_cookies()

    assert cookies == IMGAUTH
    [index] = seen
    assert index.query["action"] == "index"
    assert "Authorization" not in index.headers
    assert "session=" in index.headers["Cookie"]


async def test_only_the_signature_is_kept_as_a_secret(red) -> None:
    api, _ = await red()

    await api.image_cookies()

    assert SIGNATURE in api._secrets()
    assert base.learned_secrets() == [SIGNATURE]


async def test_the_signature_is_never_printed_even_in_debug_output(red, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", True)
    # The debug output shows one Set-Cookie header, the first: the signature's.
    api, _ = await red(cookies={"imgauth_h": SIGNATURE, "imgauth_u": "12345", "imgauth_e": "1700000000"})

    cookies = await api.image_cookies()
    await api._request("GET", f"{api.base_url}/i/cover.jpg", needs_authkey=False, cookies=cookies)

    printed = capsys.readouterr()
    assert "imgauth_h=[REDACTED]" in printed.out
    assert SIGNATURE not in printed.out + printed.err


async def test_given_cookies_go_raw_beside_the_session_cookie(red) -> None:
    api, seen = await red(cookies={})

    await api._request("GET", f"{api.base_url}/i/cover.jpg", needs_authkey=False, cookies=IMGAUTH)

    sent = dict(part.split("=", 1) for part in seen[0].headers["Cookie"].split("; "))
    assert {name: sent[name] for name in IMGAUTH} == IMGAUTH
    assert "session" in sent


async def test_an_answer_without_every_cookie_is_refused_and_nothing_is_kept(red) -> None:
    api, _ = await red(cookies={"imgauth_u": "12345"})

    with pytest.raises(RequestFailedError, match="set none of the cookies its image host takes, or not all"):
        await api.image_cookies()

    assert base.learned_secrets() == []


async def test_without_a_session_cookie_no_request_is_sent(red) -> None:
    api, seen = await red(session="")

    with pytest.raises(RequestFailedError, match="needs tracker.fakered.session, not the API key"):
        await api.image_cookies()

    assert seen == []


async def test_cookies_are_never_sent_with_the_api_key(red) -> None:
    api, seen = await red()

    with pytest.raises(ValueError, match="only sent with the session cookie"):
        await api._request("GET", f"{api.base_url}/i/x.jpg", prefer_api_key=True, needs_authkey=False, cookies=IMGAUTH)

    assert seen == []
