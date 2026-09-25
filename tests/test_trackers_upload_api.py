"""Tests for the tracker HTTP layer an upload goes through (BaseGazelleApi).

Seams used:
- For behavior *above* ``_request`` (api_call, api_key_upload, site_page_upload,
  upload dispatch, report_lossy_master, get_redirect_torrentgroupid) the tests
  replace ``api._request`` on the instance with a scripted async fake.
- For the behavior *of* ``_request`` itself (status handling, auth headers,
  retries, redirect-loop detection) the tests replace ``aiohttp.ClientSession``
  with an in-memory fake so no network is ever touched.
"""

from typing import Any, cast

import aiohttp
import asyncclick as click
import pytest
import torf
from aiohttp import web
from aiohttp.client_reqrep import ConnectionKey
from tenacity import wait_fixed

from salmon import cfg
from salmon.common import UploadFiles
from salmon.errors import LoginError, RequestError, RequestFailedError, UnknownOutcomeError, UploadError
from salmon.trackers.base import (
    BaseGazelleApi,
    HttpResponse,
    RetryableError,
    SharedLimiter,
    _redact,
)
from salmon.trackers.ops import OpsApi
from salmon.trackers.red import RedApi
from salmon.uploader import spectrals


class DummyGazelleApi(BaseGazelleApi):
    """Minimal concrete subclass so tests do not depend on tracker config."""

    def __init__(self):
        self.site_code = "DMY"
        self.base_url = "https://dummy.example"
        self.tracker_url = "https://announce.dummy.example"
        self.site_string = "Dummy"
        self.cookie = "test-cookie"
        super().__init__()


@pytest.fixture(autouse=True)
def _deterministic_cfg(monkeypatch):
    """Keep debug output off and neutralize the per-instance rate limiter."""
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", False)
    monkeypatch.setattr("salmon.trackers.base.SharedLimiter", lambda *_a, **_k: SharedLimiter(100_000, 1))


@pytest.fixture
def api() -> DummyGazelleApi:
    return DummyGazelleApi()


def script_requests(api, outcomes):
    """Replace ``api._request`` with a fake that replays ``outcomes`` in order.

    Each outcome is an HttpResponse to return or an Exception to raise.
    The last outcome is repeated if more calls arrive. Returns the list of
    recorded calls (dicts) for assertions.
    """
    calls = []

    async def fake_request(method, url, params=None, data=None, timeout_secs=10, prefer_api_key=False, idempotent=None):
        calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "data": data,
                "timeout_secs": timeout_secs,
                "prefer_api_key": prefer_api_key,
                "idempotent": idempotent,
            }
        )
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    api._request = fake_request
    return calls


def http(text="", url="https://dummy.example/x", status=200):
    return HttpResponse(text=text, url=url, status=status)


class FakeAiohttpResponse:
    def __init__(self, text="", status=200, url: Any = "https://dummy.example/x", headers=None, history=()):
        self._text = text
        self.status = status
        self.url = url
        self.headers = headers or {}
        self.history = tuple(history)

    @property
    def ok(self):
        return self.status < 400

    async def text(self):
        return self._text


class _FakeRequestCM:
    def __init__(self, outcome):
        self._outcome = outcome

    async def __aenter__(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *args):
        return False


def install_fake_aiohttp(monkeypatch, outcomes):
    """Patch aiohttp.ClientSession with an offline fake replaying ``outcomes``.

    Each outcome is a FakeAiohttpResponse to serve or an Exception to raise.
    The last outcome repeats. Returns a capture dict with the session
    constructor kwargs and the individual request calls.
    """
    captured = {"sessions": [], "requests": []}

    class FakeClientSession:
        def __init__(self, **kwargs):
            captured["sessions"].append(kwargs)
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.close()
            return False

        async def close(self):
            self.closed = True

        def request(
            self, method, url, params=None, data=None, headers=None, cookies=None, timeout=None, allow_redirects=True
        ):
            captured["requests"].append(
                {
                    "method": method,
                    "url": url,
                    "params": params,
                    "data": data,
                    "headers": headers,
                    "cookies": cookies,
                    "timeout": timeout,
                    "allow_redirects": allow_redirects,
                }
            )
            outcome = outcomes[min(len(captured["requests"]) - 1, len(outcomes) - 1)]
            return _FakeRequestCM(outcome)

    monkeypatch.setattr(aiohttp, "ClientSession", FakeClientSession)
    return captured


# ---------------------------------------------------------------------------
# _redact
# ---------------------------------------------------------------------------


def test_redact_masks_authkey_and_passkey_values():
    text = '{"authkey": "topsecret", "passkey": "alsosecret"}'
    redacted = _redact(text)
    assert "topsecret" not in redacted
    assert "alsosecret" not in redacted
    assert '"authkey": "[REDACTED]"' in redacted
    assert '"passkey": "[REDACTED]"' in redacted


def test_redact_masks_api_key_auth_and_authorization_case_insensitive():
    text = '{"API_KEY": "k1", "Auth": "k2", "authorization": "k3"}'
    redacted = _redact(text)
    for secret in ("k1", "k2", "k3"):
        assert secret not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_redact_keeps_non_sensitive_fields_untouched():
    text = '{"artist": "Testartist", "authkey": "secret"}'
    redacted = _redact(text)
    assert '"artist": "Testartist"' in redacted
    assert "secret" not in redacted


def test_redact_masks_url_query_secrets_in_html():
    html = '<a href="torrents.php?action=download&id=5&authkey=DEADBEEF&torrent_pass=PA55KEY">DL</a>'
    redacted = _redact(html)
    assert "DEADBEEF" not in redacted
    assert "PA55KEY" not in redacted
    assert "authkey=[REDACTED]" in redacted
    assert "torrent_pass=[REDACTED]" in redacted


def test_safe_response_excerpt_redacts_and_truncates():
    from salmon.trackers.base import _safe_response_excerpt

    big = "x" * 999 + "?authkey=SECRET"
    out = _safe_response_excerpt(big, limit=100)
    assert out.endswith("… [truncated]")
    assert "SECRET" not in _safe_response_excerpt("?authkey=SECRET")


# ---------------------------------------------------------------------------
# authenticate / ensure_authenticated / announce
# ---------------------------------------------------------------------------


async def test_authenticate_success_sets_keys_and_announce(api, monkeypatch):
    async def fake_api_call(action, params=None):
        assert action == "index"
        return {"authkey": "AK123", "passkey": "PK456"}

    monkeypatch.setattr(api, "api_call", fake_api_call)
    await api.authenticate()

    assert api.authkey == "AK123"
    assert api.passkey == "PK456"
    assert api._authenticated is True
    assert api.announce == "https://announce.dummy.example/PK456/announce"


async def test_ensure_authenticated_only_authenticates_once(api, monkeypatch):
    calls = []

    async def fake_api_call(action, params=None):
        calls.append(action)
        return {"authkey": "AK", "passkey": "PK"}

    monkeypatch.setattr(api, "api_call", fake_api_call)
    await api.ensure_authenticated()
    await api.ensure_authenticated()
    await api.ensure_authenticated()

    assert calls == ["index"]


async def test_authenticate_http_401_raises_login_error(api, monkeypatch):
    install_fake_aiohttp(
        monkeypatch,
        [FakeAiohttpResponse(text='{"error": "bad credentials"}', status=401)],
    )
    with pytest.raises(LoginError) as excinfo:
        await api.authenticate()
    assert "bad credentials" in str(excinfo.value)
    assert api._authenticated is False


async def test_authenticate_json_error_body_raises_request_failed(api):
    # A bad session cookie typically yields 200 + {"status": "failure"}:
    # that surfaces as RequestFailedError, not LoginError.
    script_requests(api, [http(text='{"status": "failure", "error": "This resource requires an api token"}')])
    with pytest.raises(RequestFailedError) as excinfo:
        await api.authenticate()
    assert "api token" in str(excinfo.value)


# ---------------------------------------------------------------------------
# api_call
# ---------------------------------------------------------------------------


async def test_api_call_success_returns_response_and_merges_params(api):
    calls = script_requests(api, [http(text='{"status": "success", "response": {"id": 7}}')])
    api._authenticated = True

    result = await api.api_call("torrentgroup", params={"id": 7})

    assert result == {"id": 7}
    assert calls[0]["url"] == "https://dummy.example/ajax.php"
    assert calls[0]["params"] == {"action": "torrentgroup", "id": 7}
    assert calls[0]["prefer_api_key"] is True


async def test_api_call_error_status_raises_request_failed_with_message(api):
    script_requests(api, [http(text='{"status": "failure", "error": "bad parameters"}')])
    with pytest.raises(RequestFailedError) as excinfo:
        await api.api_call("browse")
    assert str(excinfo.value) == "bad parameters"


async def test_api_call_non_json_body_raises_request_failed_with_body(api):
    script_requests(api, [http(text="<html>maintenance</html>")])
    with pytest.raises(RequestFailedError) as excinfo:
        await api.api_call("index")
    assert "<html>maintenance</html>" in str(excinfo.value)


async def test_api_call_persistent_network_error_raises_retryable_error(api, monkeypatch):
    # After 5 attempts the network failure surfaces as RetryableError, which
    # is part of the RequestError hierarchy so callers catching RequestError
    # handle it too.
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    captured = install_fake_aiohttp(monkeypatch, [aiohttp.ClientConnectionError("connection refused")])
    api._authenticated = True

    with pytest.raises(RetryableError) as excinfo:
        await api.api_call("index")

    assert isinstance(excinfo.value, RequestError)
    assert len(captured["requests"]) == 5


# ---------------------------------------------------------------------------
# _request behavior (status codes, auth headers, retry)
# ---------------------------------------------------------------------------


async def test_request_http_400_raises_request_failed(api, monkeypatch):
    install_fake_aiohttp(monkeypatch, [FakeAiohttpResponse(text='{"error": "no such action"}', status=400)])
    api._authenticated = True

    with pytest.raises(RequestFailedError) as excinfo:
        await api._request("GET", "https://dummy.example/ajax.php")
    assert "no such action" in str(excinfo.value)


async def test_request_with_api_key_uses_authorization_header_and_no_cookie(api, monkeypatch):
    captured = install_fake_aiohttp(monkeypatch, [FakeAiohttpResponse(text="ok")])
    api._authenticated = True
    api.api_key = "secret-api-key"

    await api._request("GET", "https://dummy.example/ajax.php", prefer_api_key=True)

    sent = captured["requests"][0]
    assert sent["headers"]["Authorization"] == "secret-api-key"
    assert sent["cookies"] == {}  # api-key mode sends no cookie at all


async def test_request_without_api_key_sends_the_session_cookie(api, monkeypatch):
    captured = install_fake_aiohttp(monkeypatch, [FakeAiohttpResponse(text="ok")])
    api._authenticated = True
    api.api_key = ""

    await api._request("GET", "https://dummy.example/ajax.php", prefer_api_key=True)

    sent = captured["requests"][0]
    # Off-origin hops are covered against a real server in test_trackers_session.
    assert sent["cookies"] == {"session": "test-cookie"}
    assert "Authorization" not in sent["headers"]


async def test_request_rate_limited_retries_and_succeeds(api, monkeypatch):
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    captured = install_fake_aiohttp(
        monkeypatch,
        [
            FakeAiohttpResponse(text='{"error": "rate limit exceeded"}', status=429, headers={"Retry-After": "0"}),
            FakeAiohttpResponse(text="ok", status=200),
        ],
    )
    api._authenticated = True

    resp = await api._request("GET", "https://dummy.example/ajax.php")

    assert resp.text == "ok"
    assert len(captured["requests"]) == 2


# ---------------------------------------------------------------------------
# api_key_upload
# ---------------------------------------------------------------------------


async def test_api_key_upload_success_returns_torrent_and_group_id(api):
    calls = script_requests(
        api,
        [http(text='{"status": "success", "response": {"torrentid": 123, "groupid": 456}}')],
    )
    api.authkey = "AK"
    data = {"title": "Testalbum"}

    result = await api.api_key_upload(data, UploadFiles(torrent_data=b"torrent"))

    assert result == (123, 456)
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://dummy.example/ajax.php?action=upload"
    assert calls[0]["prefer_api_key"] is True
    assert data["auth"] == "AK"


async def test_api_key_upload_success_with_camelcase_ids(api):
    script_requests(
        api,
        [http(text='{"status": "success", "response": {"torrentId": 11, "groupId": 22}}')],
    )
    result = await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert result == (11, 22)


async def test_api_key_upload_failure_json_raises_request_error_with_message(api):
    script_requests(
        api,
        [http(text='{"status": "failure", "error": "This torrent already exists"}')],
    )
    with pytest.raises(RequestError) as excinfo:
        await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert "This torrent already exists" in str(excinfo.value)


async def test_api_key_upload_failure_json_without_error_key_raises_request_error(api):
    # A failure body without an "error" key still raises RequestError with
    # the raw response content in the message.
    script_requests(api, [http(text='{"status": "failure"}')])
    with pytest.raises(RequestError) as excinfo:
        await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert "API upload failed" in str(excinfo.value)
    assert "'status': 'failure'" in str(excinfo.value)


async def test_api_key_upload_non_json_response_raises_abort(api):
    script_requests(api, [http(text="<html><body>Cloudflare says no</body></html>")])
    with pytest.raises(click.Abort):
        await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))


async def test_api_key_upload_non_dict_json_raises_request_error(api):
    script_requests(api, [http(text="[1, 2, 3]")])
    with pytest.raises(RequestError) as excinfo:
        await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert "API upload failed" in str(excinfo.value)


async def test_api_key_upload_http_error_status_raises_request_failed(api, monkeypatch):
    install_fake_aiohttp(monkeypatch, [FakeAiohttpResponse(text='{"error": "upload disabled"}', status=403)])
    api._authenticated = True

    with pytest.raises(RequestFailedError) as excinfo:
        await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert "upload disabled" in str(excinfo.value)


async def test_api_key_upload_filled_request_reports_url(api, capsys):
    script_requests(
        api,
        [http(text='{"status": "success", "response": {"requestid": 55, "torrentid": 1, "groupid": 2}}')],
    )
    result = await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))

    assert result == (1, 2)
    out = capsys.readouterr().out
    assert "Filled request" in out
    assert "https://dummy.example/requests.php?action=view&id=55" in out


async def test_api_key_upload_request_fill_failed_returns_zero_ids(api, capsys):
    # requestid == -1 signals a failed request fill; this alternate response
    # shape legitimately carries no torrent ids, so the caller gets (0, 0).
    script_requests(api, [http(text='{"status": "success", "response": {"requestid": -1}}')])
    result = await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))

    assert result == (0, 0)
    assert "Request fill failed!" in capsys.readouterr().out


async def test_api_key_upload_fill_request_shape_reports_url_and_returns_ids(api, capsys):
    script_requests(
        api,
        [
            http(
                text='{"status": "success", "response": '
                '{"fillRequest": {"requestId": 77}, "torrentId": 5, "groupId": 6}}'
            )
        ],
    )
    result = await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))

    assert result == (5, 6)
    out = capsys.readouterr().out
    assert "Filled request" in out
    assert "https://dummy.example/requests.php?action=view&id=77" in out


async def test_api_key_upload_success_without_torrent_id_raises_upload_error(api):
    # A success response with no torrent id and no request-fill marker must
    # not surface as (0, 0) — downstream would treat 0 as a real torrent id.
    script_requests(api, [http(text='{"status": "success", "response": {}}')])
    with pytest.raises(UploadError) as excinfo:
        await api.api_key_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert "no torrent id" in str(excinfo.value)
    assert "'status': 'success'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# site_page_upload
# ---------------------------------------------------------------------------

GROUP_PAGE_HTML = """
<html><body>
<a class="tooltip" href="torrents.php?torrentid=111">older torrent</a>
<a class="tooltip" href="torrents.php?torrentid=222">our new torrent</a>
<a class="brackets" href="upload.php?groupid=333">[Add format]</a>
</body></html>
"""


async def test_site_page_upload_success_parses_newest_torrent_and_group_id(api):
    calls = script_requests(api, [http(text=GROUP_PAGE_HTML, url="https://dummy.example/torrents.php?id=333")])
    api.authkey = "AK"
    api.passkey = "PK"
    data = {"title": "Testalbum"}

    result = await api.site_page_upload(data, UploadFiles(torrent_data=b"torrent"))

    assert result == (222, 333)
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://dummy.example/upload.php"
    assert data["auth"] == "AK"


async def test_site_page_upload_with_groupid_posts_to_group_upload_url(api):
    calls = script_requests(api, [http(text=GROUP_PAGE_HTML, url="https://dummy.example/torrents.php?id=333")])
    api.authkey = "AK"
    api.passkey = "PK"

    await api.site_page_upload({"groupid": 333}, UploadFiles(torrent_data=b"torrent"))

    assert calls[0]["url"] == "https://dummy.example/upload.php?groupid=333"


async def test_site_page_upload_failure_page_extracts_red_error(api):
    api.passkey = "PK"
    failure_html = (
        f"<html><body><input value='{api.announce}' />"
        '<p style="color: red; text-align: center;">No torrent file uploaded, or file empty.</p>'
        "</body></html>"
    )
    script_requests(api, [http(text=failure_html, url="https://dummy.example/upload.php", status=200)])

    with pytest.raises(RequestError) as excinfo:
        await api.site_page_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert "Site upload failed: No torrent file uploaded, or file empty." in str(excinfo.value)


async def test_site_page_upload_unparseable_page_raises_request_error(api):
    api.passkey = "PK"
    script_requests(api, [http(text="<html><body>login page</body></html>", url="https://dummy.example/login.php")])

    with pytest.raises(RequestError) as excinfo:
        await api.site_page_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert "Site upload failed, response text" in str(excinfo.value)


async def test_site_page_upload_request_fill_success_resolves_group_via_redirect(api):
    fill_html = '<html><body><a href="torrents.php?torrentid=789">Yes</a></body></html>'
    calls = script_requests(
        api,
        [
            http(text=fill_html, url="https://dummy.example/requests.php?action=view&id=77"),
            http(text="", url="https://dummy.example/torrents.php?id=456&torrentid=789"),
        ],
    )
    api.authkey = "AK"
    api.passkey = "PK"

    result = await api.site_page_upload({}, UploadFiles(torrent_data=b"torrent"))

    assert result == (789, 456)
    # Second call resolved the group id from the torrent redirect.
    assert calls[1]["params"] == {"torrentid": 789}


async def test_site_page_upload_request_fill_failure_extracts_error(api):
    fill_error_html = "<html><body><div><div><h2>Error</h2></div><p>Request already filled</p></div></body></html>"
    script_requests(api, [http(text=fill_error_html, url="https://dummy.example/requests.php?action=takefill")])
    api.passkey = "PK"

    with pytest.raises(RequestError) as excinfo:
        await api.site_page_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert "Request fill failed: Request already filled" in str(excinfo.value)


async def test_site_page_upload_bounced_to_login_raises_login_error(api, monkeypatch):
    # An expired cookie sends the upload to login.php, which is never requested.
    captured = install_fake_aiohttp(
        monkeypatch,
        [FakeAiohttpResponse(status=302, url="https://dummy.example/upload.php", headers={"Location": "/login.php"})],
    )
    api._authenticated = True
    api.passkey = "PK"

    with pytest.raises(LoginError):
        await api.site_page_upload({}, UploadFiles(torrent_data=b"torrent"))
    assert len(captured["requests"]) == 1


# ---------------------------------------------------------------------------
# upload() dispatch
# ---------------------------------------------------------------------------


async def test_upload_dispatches_to_api_key_upload_when_api_key_set(api):
    calls = []

    async def fake_api_key_upload(data, files):
        calls.append("api")
        return (1, 2)

    async def fake_site_page_upload(data, files):
        calls.append("site")
        return (3, 4)

    api.api_key = "some-key"
    api.api_key_upload = fake_api_key_upload
    api.site_page_upload = fake_site_page_upload

    result = await api.upload({}, UploadFiles(torrent_data=b"torrent"))

    assert result == (1, 2)
    assert calls == ["api"]


async def test_upload_dispatches_to_site_page_upload_without_api_key(api):
    calls = []

    async def fake_api_key_upload(data, files):
        calls.append("api")
        return (1, 2)

    async def fake_site_page_upload(data, files):
        calls.append("site")
        return (3, 4)

    api.api_key = ""
    api.api_key_upload = fake_api_key_upload
    api.site_page_upload = fake_site_page_upload

    result = await api.upload({}, UploadFiles(torrent_data=b"torrent"))

    assert result == (3, 4)
    assert calls == ["site"]


def _patch_upload_recorders(monkeypatch, calls):
    async def fake_api_key_upload(self, data, files):
        calls.append("api")
        return (1, 2)

    async def fake_site_page_upload(self, data, files):
        calls.append("site")
        return (3, 4)

    monkeypatch.setattr(BaseGazelleApi, "api_key_upload", fake_api_key_upload)
    monkeypatch.setattr(BaseGazelleApi, "site_page_upload", fake_site_page_upload)


async def test_red_upload_with_log_files_forces_site_page_upload_despite_api_key(monkeypatch):
    calls = []
    _patch_upload_recorders(monkeypatch, calls)
    red = RedApi()
    red.api_key = "red-api-key"
    files = UploadFiles(torrent_data=b"torrent", log_files=[("rip.log", b"EAC log")])

    result = await red.upload({}, files)

    assert result == (3, 4)
    assert calls == ["site"]


async def test_red_upload_without_log_files_uses_api_key_upload(monkeypatch):
    calls = []
    _patch_upload_recorders(monkeypatch, calls)
    red = RedApi()
    red.api_key = "red-api-key"

    result = await red.upload({}, UploadFiles(torrent_data=b"torrent"))

    assert result == (1, 2)
    assert calls == ["api"]


async def test_red_upload_without_api_key_uses_site_page_upload(monkeypatch):
    calls = []
    _patch_upload_recorders(monkeypatch, calls)
    red = RedApi()
    red.api_key = ""

    result = await red.upload({}, UploadFiles(torrent_data=b"torrent"))

    assert result == (3, 4)
    assert calls == ["site"]


RED_UPLOAD_FORM_HTML = """
<html><body><form>
<input name="artists[]" value="Test Artist" />
<select name="importance[]"><option value="1" selected>Main</option></select>
<input name="title" value="Existing Album" />
<input name="year" value="2020" />
<input name="tags" value="electronic" />
<select name="releasetype"><option value="1" selected>Album</option></select>
<textarea name="album_desc">Great album</textarea>
</form></body></html>
"""


async def test_red_site_page_upload_enriches_data_from_group_form(monkeypatch):
    red = RedApi()
    red.api_key = ""
    red.authkey = "AK"
    red.passkey = "PK"
    calls = script_requests(
        red,
        [
            http(text=RED_UPLOAD_FORM_HTML, url="https://redacted.sh/upload.php?groupid=42"),
            http(text=GROUP_PAGE_HTML, url="https://redacted.sh/torrents.php?id=333"),
        ],
    )
    data = {"groupid": 42}

    result = await red.upload(data, UploadFiles(torrent_data=b"torrent"))

    assert result == (222, 333)
    # First request scraped the pre-filled upload form for the group.
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://redacted.sh/upload.php?groupid=42"
    assert data["artists[]"] == ["Test Artist"]
    assert data["importance[]"] == [1]
    assert data["title"] == "Existing Album"
    assert data["year"] == "2020"
    assert data["releasetype"] == "1"
    assert data["album_desc"] == "Great album"
    # Second request was the actual POST to the group upload URL.
    assert calls[1]["method"] == "POST"
    assert calls[1]["url"] == "https://redacted.sh/upload.php?groupid=42"


# ---------------------------------------------------------------------------
# report_lossy_master
# ---------------------------------------------------------------------------


async def test_report_lossy_master_web_source_succeeds(api):
    calls = script_requests(api, [http(url="https://dummy.example/torrents.php?id=1")])
    api.authkey = "AK"

    result = await api.report_lossy_master(42, "lossy comment", "WEB")

    assert result is True
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://dummy.example/reportsv2.php"
    assert call["params"] == {"action": "takereport"}
    assert call["data"]["type"] == "lossywebapproval"
    assert call["data"]["torrentid"] == 42
    assert call["data"]["extra"] == "lossy comment"
    assert call["data"]["auth"] == "AK"


async def test_report_lossy_master_non_web_source_uses_lossyapproval(api):
    calls = script_requests(api, [http(url="https://dummy.example/torrents.php?id=1")])
    await api.report_lossy_master(42, "comment", "CD")
    assert calls[0]["data"]["type"] == "lossyapproval"


async def test_report_lossy_master_failure_raises_request_error(api):
    script_requests(api, [http(url="https://dummy.example/reportsv2.php", status=200)])
    with pytest.raises(RequestError) as excinfo:
        await api.report_lossy_master(42, "comment", "WEB")
    assert "Failed to report the torrent for lossy master" in str(excinfo.value)


# ---------------------------------------------------------------------------
# append_to_torrent_description
# ---------------------------------------------------------------------------

TORRENT_DETAILS_JSON = (
    '{"status": "success", "response": {"torrent": {'
    '"remasterYear": 2020, "remasterTitle": "", "remasterRecordLabel": "", '
    '"remasterCatalogueNumber": "", "format": "FLAC", "encoding": "Lossless", '
    '"media": "WEB", "description": "Old description"}}}'
)


async def test_append_to_torrent_description_success_prepends_text(api, capsys):
    calls = script_requests(
        api,
        [
            http(text=TORRENT_DETAILS_JSON),
            http(text="<html><body><h2>Edit successful</h2></body></html>"),
        ],
    )
    api.authkey = "AK"

    await api.append_to_torrent_description(42, "Spectrals: ")

    assert calls[1]["method"] == "POST"
    assert calls[1]["url"] == "https://dummy.example/torrents.php"
    assert calls[1]["data"]["release_desc"] == "Spectrals: Old description"
    assert "Added spectrals to the torrent description." in capsys.readouterr().out


async def test_append_to_torrent_description_error_page_raises_request_error(api):
    error_html = "<html><body><div><div><h2>Error</h2></div><p>No changes detected</p></div></body></html>"
    script_requests(api, [http(text=TORRENT_DETAILS_JSON), http(text=error_html)])
    api.authkey = "AK"

    with pytest.raises(RequestError) as excinfo:
        await api.append_to_torrent_description(42, "Spectrals: ")
    assert "Failed to edit torrent: No changes detected" in str(excinfo.value)


# ---------------------------------------------------------------------------
# get_redirect_torrentgroupid
# ---------------------------------------------------------------------------


async def test_get_redirect_torrentgroupid_found_returns_int_group_id(api):
    calls = script_requests(api, [http(url="https://dummy.example/torrents.php?id=999&torrentid=5")])

    result = await api.get_redirect_torrentgroupid(5)

    assert result == 999
    assert calls[0]["params"] == {"torrentid": 5}


async def test_get_redirect_torrentgroupid_without_redirect_raises_abort(api, capsys):
    script_requests(api, [http(url="https://dummy.example/torrents.php?torrentid=5")])

    with pytest.raises(click.Abort):
        await api.get_redirect_torrentgroupid(5)
    assert "no Redirect found" in capsys.readouterr().out


async def test_get_redirect_torrentgroupid_timeout_raises_abort(api, capsys):
    script_requests(api, [TimeoutError("timed out")])

    with pytest.raises(click.Abort):
        await api.get_redirect_torrentgroupid(5)
    assert "timed out" in capsys.readouterr().out


async def test_label_rls_fetches_every_page_exactly_once(api):
    pages_called = []

    def group(n):
        return {
            "artist": f"A{n}",
            "groupYear": 2020,
            "groupName": f"G{n}",
            "releaseType": 1,
            "groupId": n,
            "torrents": [{"format": "FLAC", "media": "WEB"}],
        }

    async def fake_api_call(action, params=None):
        assert action == "browse"
        page = int((params or {}).get("page", 1))
        pages_called.append(page)
        return {"pages": 3, "results": [group(page)]}

    api.api_call = fake_api_call
    releases = await api.label_rls("Label")

    # Every page exactly once: no last-page skip, no duplicate page-1 fetch.
    assert sorted(pages_called) == [1, 2, 3]
    assert [r.url for r in releases] == [f"{api.base_url}/torrents.php?id={n}" for n in (1, 2, 3)]


async def test_request_refuses_off_origin_redirect(api, monkeypatch):
    hop = FakeAiohttpResponse(
        status=302, url="https://dummy.example/torrents.php", headers={"Location": "https://evil.example/login"}
    )
    captured = install_fake_aiohttp(monkeypatch, [hop, FakeAiohttpResponse(text="pwned")])
    api._authenticated = True

    with pytest.raises(RequestFailedError, match="another site"):
        await api._request("GET", "https://dummy.example/torrents.php")
    assert len(captured["requests"]) == 1


async def test_request_same_origin_redirect_is_allowed(api, monkeypatch):
    hop = FakeAiohttpResponse(
        status=302, url="https://dummy.example/torrents.php?torrentid=1", headers={"Location": "torrents.php?id=2"}
    )
    final = FakeAiohttpResponse(text="ok", url="https://dummy.example/torrents.php?id=2")
    captured = install_fake_aiohttp(monkeypatch, [hop, final])
    api._authenticated = True

    resp = await api._request("GET", "https://dummy.example/torrents.php", params={"torrentid": 1})
    assert resp.text == "ok"
    assert [r["url"] for r in captured["requests"]] == [
        "https://dummy.example/torrents.php",
        "https://dummy.example/torrents.php?id=2",
    ]


def test_redact_masks_cookie_credentials():
    set_cookie = "Set-Cookie: session=abc123; Path=/; HttpOnly"
    assert "abc123" not in _redact(set_cookie)

    json_headers = '{"Set-Cookie": "keeplogged=deadbeef; expires=never", "session": "s3cr3t"}'
    redacted = _redact(json_headers)
    assert "deadbeef" not in redacted
    assert "s3cr3t" not in redacted


# ---------------------------------------------------------------------------
# A request that changes state is never sent twice once it may have reached the tracker
# ---------------------------------------------------------------------------


def _connect_refused() -> aiohttp.ClientConnectorError:
    key = ConnectionKey("dummy.example", 443, True, True, None, None, None)
    return aiohttp.ClientConnectorError(key, OSError(61, "Connection refused"))


def _real_torrent(tmp_path) -> bytes:
    (tmp_path / "track.flac").write_bytes(b"x" * 1000)
    torrent = torf.Torrent(path=tmp_path, trackers=["https://announce.dummy.example"], private=True)
    torrent.generate()
    return torrent.dump()


@pytest.mark.parametrize(
    "outcome",
    [
        aiohttp.ServerDisconnectedError(),
        TimeoutError(),
        FakeAiohttpResponse(text="bad gateway", status=502),
        FakeAiohttpResponse(text="unavailable", status=503),
        FakeAiohttpResponse(text="not implemented", status=501),
        FakeAiohttpResponse(text="origin timed out", status=524),
    ],
    ids=["answer-dropped", "timeout", "502", "503", "501", "524"],
)
async def test_a_post_that_may_have_reached_the_tracker_is_sent_once(api, monkeypatch, outcome):
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    captured = install_fake_aiohttp(monkeypatch, [outcome])
    api._authenticated = True

    with pytest.raises(UnknownOutcomeError):
        await api._request("POST", "https://dummy.example/upload.php", data={"x": "1"})
    assert len(captured["requests"]) == 1


async def test_an_unusual_5xx_on_a_get_fails_without_a_retry(api, monkeypatch):
    captured = install_fake_aiohttp(monkeypatch, [FakeAiohttpResponse(text="not implemented", status=501)])
    api._authenticated = True

    with pytest.raises(RequestFailedError):
        await api._request("GET", "https://dummy.example/ajax.php")
    assert len(captured["requests"]) == 1


async def test_a_post_that_never_connected_is_retried(api, monkeypatch):
    # Nothing reached the tracker, so sending it again cannot do anything twice.
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    captured = install_fake_aiohttp(monkeypatch, [_connect_refused()])
    api._authenticated = True

    with pytest.raises(RetryableError):
        await api._request("POST", "https://dummy.example/upload.php", data={"x": "1"})
    assert len(captured["requests"]) == 5


def _record_sleeps(monkeypatch) -> list[float]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        # tenacity's own wait_fixed(0) between attempts sleeps too; only real waits count.
        if seconds:
            sleeps.append(seconds)

    monkeypatch.setattr("salmon.trackers.base.asyncio.sleep", sleep)
    return sleeps


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [(None, 20.0), ("7", 7.0), ("-5", 20.0), ("not a number", 20.0), ("Wed, 21 Oct 2099 07:28:00 GMT", 120.0)],
    ids=["missing", "seconds", "negative", "garbage", "far-future date"],
)
async def test_a_429_waits_for_any_retry_after_form(api, monkeypatch, retry_after, expected):
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    sleeps = _record_sleeps(monkeypatch)
    headers = {"Retry-After": retry_after} if retry_after else {}
    install_fake_aiohttp(
        monkeypatch, [FakeAiohttpResponse(status=429, headers=headers), FakeAiohttpResponse(text="ok")]
    )
    api._authenticated = True

    resp = await api._request("GET", "https://dummy.example/ajax.php")
    assert resp.text == "ok"
    assert sleeps == [expected]


@pytest.mark.parametrize(("method", "expected"), [("GET", [3.0]), ("POST", [])])
async def test_a_5xx_retry_after_is_honoured_only_when_retried(api, monkeypatch, method, expected):
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    sleeps = _record_sleeps(monkeypatch)
    captured = install_fake_aiohttp(
        monkeypatch, [FakeAiohttpResponse(status=503, headers={"Retry-After": "3"}), FakeAiohttpResponse(text="ok")]
    )
    api._authenticated = True

    if method == "POST":
        # Never re-sent: the tracker may have acted on it.
        with pytest.raises(UnknownOutcomeError):
            await api._request(method, "https://dummy.example/ajax.php")
        assert len(captured["requests"]) == 1
    else:
        resp = await api._request(method, "https://dummy.example/ajax.php")
        assert resp.text == "ok"
    assert sleeps == expected


async def test_a_post_refused_with_429_is_retried(api, monkeypatch):
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    captured = install_fake_aiohttp(
        monkeypatch,
        [FakeAiohttpResponse(status=429, headers={"Retry-After": "0"}), FakeAiohttpResponse(text="ok")],
    )
    api._authenticated = True

    resp = await api._request("POST", "https://dummy.example/upload.php", data={"x": "1"})
    assert resp.text == "ok"
    assert len(captured["requests"]) == 2


@pytest.mark.parametrize("status", [429, 401, 403, 404])
async def test_a_failure_after_the_post_was_redirected_is_an_unknown_outcome(api, monkeypatch, status):
    # A redirect means the tracker already acted on the POST, whatever the later hop answers.
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    hop = FakeAiohttpResponse(
        status=302, url="https://dummy.example/upload.php", headers={"Location": "/torrents.php?id=5"}
    )
    captured = install_fake_aiohttp(
        monkeypatch,
        [
            hop,
            FakeAiohttpResponse(status=status, headers={"Retry-After": "0"}, url="https://dummy.example/torrents.php"),
        ],
    )
    api._authenticated = True

    with pytest.raises(UnknownOutcomeError):
        await api._request("POST", "https://dummy.example/upload.php", data={"x": "1"})
    assert [r["method"] for r in captured["requests"]] == ["POST", "GET"]


def _hop(url: str, location: str) -> FakeAiohttpResponse:
    return FakeAiohttpResponse(status=302, url=url, headers={"Location": location})


@pytest.mark.parametrize(
    "later",
    [
        [_hop("https://dummy.example/torrents.php", "/login.php")],
        [_hop("https://dummy.example/torrents.php", "https://evil.example/x")],
        [_hop("https://dummy.example/torrents.php", "/torrents.php")],
    ],
    ids=["login-bounce", "off-site", "too-many-hops"],
)
async def test_a_refused_hop_after_the_post_was_redirected_is_an_unknown_outcome(api, monkeypatch, later):
    captured = install_fake_aiohttp(monkeypatch, [_hop("https://dummy.example/upload.php", "/torrents.php"), *later])
    api._authenticated = True

    with pytest.raises(UnknownOutcomeError):
        await api._request("POST", "https://dummy.example/upload.php", data={"x": "1"})
    assert captured["requests"][0]["method"] == "POST"
    assert len(captured["requests"]) <= 4


async def test_a_post_redirected_straight_off_site_is_an_unknown_outcome(api, monkeypatch):
    # The redirect is the tracker's answer, so it has acted, even though the hop is refused.
    install_fake_aiohttp(monkeypatch, [_hop("https://dummy.example/upload.php", "https://evil.example/x")])
    api._authenticated = True

    with pytest.raises(UnknownOutcomeError):
        await api._request("POST", "https://dummy.example/upload.php", data={"x": "1"})


async def test_a_post_bounced_straight_to_login_stays_a_login_error(api, monkeypatch):
    install_fake_aiohttp(monkeypatch, [_hop("https://dummy.example/upload.php", "/login.php")])
    api._authenticated = True

    with pytest.raises(LoginError):
        await api._request("POST", "https://dummy.example/upload.php", data={"x": "1"})


async def test_an_idempotent_post_keeps_retrying(api, monkeypatch):
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    captured = install_fake_aiohttp(monkeypatch, [aiohttp.ServerDisconnectedError()])
    api._authenticated = True

    with pytest.raises(RetryableError):
        await api._request("POST", "https://dummy.example/torrents.php", data={"x": "1"}, idempotent=True)
    assert len(captured["requests"]) == 5


async def test_a_get_whose_answer_is_dropped_is_still_retried(api, monkeypatch):
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    captured = install_fake_aiohttp(monkeypatch, [aiohttp.ServerDisconnectedError()])
    api._authenticated = True

    with pytest.raises(RetryableError):
        await api._request("GET", "https://dummy.example/ajax.php")
    assert len(captured["requests"]) == 5


@pytest.mark.parametrize("method", ["api_key_upload", "site_page_upload"])
async def test_a_lost_upload_found_by_its_infohash_returns_its_ids(api, tmp_path, method):
    torrent = _real_torrent(tmp_path)
    found = '{"status": "success", "response": {"torrent": {"id": 123}, "group": {"id": 456}}}'
    calls = script_requests(api, [UnknownOutcomeError("answer lost"), http(text=found)])
    api.authkey = "AK"

    result = await getattr(api, method)({}, UploadFiles(torrent_data=torrent))

    assert result == (123, 456)
    assert len(calls) == 2
    assert calls[0]["method"] == "POST"
    assert calls[1]["method"] == "GET"
    assert calls[1]["params"]["action"] == "torrent"
    assert calls[1]["params"]["hash"] == torf.Torrent.read_stream(torrent).infohash.upper()


@pytest.mark.parametrize("method", ["api_key_upload", "site_page_upload"])
async def test_a_lost_upload_not_found_says_it_may_have_gone_through(api, tmp_path, method):
    torrent = _real_torrent(tmp_path)
    calls = script_requests(api, [UnknownOutcomeError("answer lost"), http(text='{"status": "failure"}')])
    api.authkey = "AK"

    with pytest.raises(UnknownOutcomeError) as excinfo:
        await getattr(api, method)({}, UploadFiles(torrent_data=torrent))

    assert "may still have gone through" in str(excinfo.value)
    # The lookup may have failed rather than come back empty.
    assert "did not confirm it" in str(excinfo.value)
    assert [c["method"] for c in calls] == ["POST", "GET"]


async def test_the_description_edit_is_sent_as_idempotent(api):
    torrent = (
        '{"remasterYear": 2020, "remasterTitle": "", "remasterRecordLabel": "", "remasterCatalogueNumber": "",'
        ' "format": "FLAC", "encoding": "Lossless", "media": "WEB", "description": "old"}'
    )
    group = '{"status": "success", "response": {"torrent": ' + torrent + ', "group": {"id": 1}}}'
    calls = script_requests(api, [http(text=group), http(text="<html></html>")])
    api.authkey = "AK"

    await api.append_to_torrent_description(7, "addition")

    assert calls[-1]["method"] == "POST"
    assert calls[-1]["idempotent"] is True


@pytest.fixture
async def tracker_that_drops_answers():
    """A real tracker that reads each request in full, then drops the connection unanswered."""
    hits: list[str] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        await request.read()
        hits.append(request.method)
        assert request.transport is not None
        request.transport.close()
        return web.Response()

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield f"http://127.0.0.1:{runner.addresses[0][1]}", hits
    await runner.cleanup()


async def test_a_real_dropped_answer_to_a_post_is_not_resent(api, monkeypatch, tracker_that_drops_answers):
    # Pins the exception aiohttp really raises here, which the fakes above can only assume.
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    base, hits = tracker_that_drops_answers
    api.base_url = base
    api._authenticated = True

    with pytest.raises(UnknownOutcomeError):
        await api._request("POST", f"{base}/upload.php", data={"x": "1"})
    assert hits == ["POST"]


async def test_a_real_dropped_answer_to_a_get_is_retried(api, monkeypatch, tracker_that_drops_answers):
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    base, hits = tracker_that_drops_answers
    api.base_url = base
    api._authenticated = True

    try:
        with pytest.raises(RetryableError):
            await api._request("GET", f"{base}/ajax.php")
    finally:
        await api.close()
    # aiohttp also re-sends an idempotent request once itself, so each attempt can hit twice.
    assert len(hits) >= 5
    assert set(hits) == {"GET"}


async def test_a_report_with_an_unknown_outcome_is_not_filed_again(capsys):
    class _Site:
        base_url = "https://dummy.example"

        async def report_lossy_master(self, *_args):
            raise UnknownOutcomeError("answer lost")

    await spectrals.report_lossy_master(cast("Any", _Site()), 7, None, None, "WEB", "comment")

    out = capsys.readouterr().out
    assert "Could not tell whether the Lossy Master/WEB report was filed" in out
    assert "torrents.php?torrentid=7" in out
    assert "Reported upload for Lossy Master/WEB Approval Request" not in out


async def test_opss_own_report_is_not_resent_either(monkeypatch):
    # OPS overrides report_lossy_master with its own takereport POST.
    monkeypatch.setattr(cast("Any", BaseGazelleApi._request).retry, "wait", wait_fixed(0))
    captured = install_fake_aiohttp(monkeypatch, [aiohttp.ServerDisconnectedError()])
    ops = OpsApi()
    ops._authenticated = True
    ops.authkey = "AK"
    ops.cookie = "test-cookie"
    ops.keeplogged = None

    with pytest.raises(UnknownOutcomeError):
        await ops.report_lossy_master(7, "comment", "WEB")
    assert len(captured["requests"]) == 1


async def test_a_failed_lookup_cannot_leak_credentials(api, tmp_path):
    # A non-JSON answer reaches the message whole, and a logged-in page's links carry secrets.
    torrent = _real_torrent(tmp_path)
    page = '<a href="torrents.php?action=download&authkey=SYNTHETIC-AUTHKEY&torrent_pass=SYNTHETIC-PASS">DL</a>'
    script_requests(api, [UnknownOutcomeError("answer lost"), http(text=page)])
    api.authkey = "AK"

    with pytest.raises(UnknownOutcomeError) as excinfo:
        await api.api_key_upload({}, UploadFiles(torrent_data=torrent))

    message = str(excinfo.value)
    assert "SYNTHETIC-AUTHKEY" not in message
    assert "SYNTHETIC-PASS" not in message
    assert "may still have gone through" in message
