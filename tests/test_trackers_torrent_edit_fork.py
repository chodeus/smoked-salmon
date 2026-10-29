"""What this fork's description edit adds to upstream's: it authenticates first, and masks and caps what it prints."""

from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from aiohttp import web
from tenacity import wait_fixed

from salmon import cfg
from salmon.errors import RequestError
from salmon.trackers.account import SlidingWindowLimiter
from salmon.trackers.base import BaseGazelleApi

# RED's and OPS's edit pages come from real pages, DIC's is reconstructed; each expected POST is what a browser sends.
FIXTURES = Path(__file__).parent / "fixtures" / "torrent_edit"

ADDITION = "[hide=Spectrals][b]01 Full[/b]\n[img=https://img.example/1.png]\n[/hide]\n"

ERROR_PAGE = """<html><body><div id="content"><div class="thin"><h2>Error</h2>
<div class="box pad"><p>You do not have permission to edit this torrent.</p></div></div></div></body></html>"""


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _fast(monkeypatch) -> None:
    monkeypatch.setattr(cfg.tracker, "debug_connection", False)
    monkeypatch.setattr(BaseGazelleApi, "_rate_limiter", SlidingWindowLimiter(100, 1))
    monkeypatch.setattr(cast("Any", BaseGazelleApi._send).retry, "wait", wait_fixed(0))


class FakeTracker:
    """A local tracker serving one edit page and the API answer for the same torrent."""

    def __init__(self, edit_page: str, edit_status: int = 200, edit_answer: str | None = None) -> None:
        self.edit_page = edit_page
        self.edit_status = edit_status
        # What the tracker answers the POST with instead of its redirect to the group, if set.
        self.edit_answer = edit_answer
        self.requests: list[tuple[str, str]] = []
        self.posts: list[dict[str, str]] = []
        self._runner: web.AppRunner | None = None

    async def _torrents(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.method, request.path_qs))
        if request.method == "POST":
            form = await request.post()
            fields = [(name, str(value)) for name, value in form.items()]
            posted = dict(fields)
            assert len(posted) == len(fields), f"a field was sent twice: {fields}"
            self.posts.append(posted)
            if self.edit_answer is not None:
                return web.Response(text=self.edit_answer, content_type="text/html")
            raise web.HTTPFound("/torrents.php?id=1000")
        if request.query.get("action") == "edit":
            if self.edit_status == 302:
                raise web.HTTPFound("/login.php")
            return web.Response(text=self.edit_page, content_type="text/html")
        return web.Response(text="<html><body><h2>Group page</h2></body></html>", content_type="text/html")

    authkey = "XXXX"

    async def _ajax(self, request: web.Request) -> web.Response:
        self.requests.append((request.method, request.path_qs))
        return web.json_response({"status": "success", "response": {"authkey": self.authkey, "passkey": "p"}})

    async def __aenter__(self) -> "FakeTracker":
        app = web.Application()
        app.router.add_route("*", "/torrents.php", self._torrents)
        app.router.add_route("*", "/ajax.php", self._ajax)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._runner is not None
        await self._runner.cleanup()

    @property
    def url(self) -> str:
        assert self._runner is not None
        return f"http://127.0.0.1:{self._runner.addresses[0][1]}"


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    tracker_url = "https://announce.fake.example"

    def __init__(self, base_url: str, authenticated: bool = True) -> None:
        self.base_url = base_url
        self.cookie = "fake-cookie"
        super().__init__()
        # As every caller is: the upload, or checkspecs's torrent lookup, authenticated the client first.
        if authenticated:
            self._authenticated = True
            self.authkey = "XXXX"


async def _edit(tracker: FakeTracker, torrent_id: int, authenticated: bool = True) -> None:
    async with tracker:
        api = FakeApi(tracker.url, authenticated)
        try:
            await api.append_to_torrent_description(torrent_id, ADDITION)
        finally:
            await api.close()


def _run_edit(tracker: FakeTracker, torrent_id: int = 2001, authenticated: bool = True) -> None:
    anyio.run(_edit, tracker, torrent_id, authenticated)


def test_a_fresh_client_authenticates_before_reading_the_edit_page() -> None:
    fake = FakeTracker(_fixture("red_edit.html"))
    _run_edit(fake, authenticated=False)
    assert fake.requests[:2] == [("GET", "/ajax.php?action=index"), ("GET", "/torrents.php?action=edit&id=2001")]


def test_debug_output_never_shows_the_edit_forms_auth(monkeypatch, capsys) -> None:
    # The form's auth is the client's authkey, masked as a known secret once the client is authenticated.
    secret = "edit-form-auth-0123456789"
    monkeypatch.setattr(cfg.tracker, "debug_connection", True)
    monkeypatch.setattr(FakeTracker, "authkey", secret, raising=False)
    fake = FakeTracker(_fixture("red_edit.html").replace('value="XXXX"', f'value="{secret}"'))
    _run_edit(fake, authenticated=False)
    assert fake.posts[0]["auth"] == secret
    out = capsys.readouterr().out
    assert "[DEBUG] response body" in out
    assert secret not in out


def test_debug_output_masks_a_form_auth_the_client_does_not_know(monkeypatch, capsys) -> None:
    secret = "edit-form-auth-0123456789"
    monkeypatch.setattr(cfg.tracker, "debug_connection", True)
    monkeypatch.setattr(FakeTracker, "authkey", "another-authkey-9876543210", raising=False)
    fake = FakeTracker(_fixture("red_edit.html").replace('value="XXXX"', f'value="{secret}"'))
    _run_edit(fake, authenticated=False)
    out = capsys.readouterr().out
    assert "[DEBUG] response body" in out
    assert secret not in out


@pytest.mark.parametrize(
    "tag",
    [
        '<input type="hidden" name="auth" value="SECRET-123">',
        "<input value='SECRET-123' type='hidden' name='torrent_pass'/>",
        '<INPUT NAME="authkey" VALUE="SECRET-123">',
    ],
)
def test_redaction_masks_a_secret_form_input_either_way_round(tag: str) -> None:
    from salmon.common.redaction import redact_tracker_text

    redacted = redact_tracker_text(f'<form>{tag}<input name="title" value="Album"></form>')
    assert "SECRET-123" not in redacted
    assert 'value="Album"' in redacted


def test_a_long_refusal_from_the_tracker_is_capped() -> None:
    fake = FakeTracker(_fixture("red_edit.html"), edit_answer=ERROR_PAGE.replace("You do not", "x" * 2000))
    with pytest.raises(RequestError, match=r"\[truncated\]"):
        _run_edit(fake)
