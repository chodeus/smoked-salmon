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
from aiolimiter import AsyncLimiter
from tenacity import wait_fixed

from salmon import cfg
from salmon.common import UploadFiles
from salmon.errors import LoginError, RequestError, RequestFailedError, UploadError
from salmon.trackers.base import (
    BaseGazelleApi,
    HttpResponse,
    RetryableError,
    _redact,
)
from salmon.trackers.red import RedApi


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
    monkeypatch.setattr("salmon.trackers.base.AsyncLimiter", lambda *_a, **_k: AsyncLimiter(100_000, 1))


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

    async def fake_request(method, url, params=None, data=None, timeout_secs=10, prefer_api_key=False):
        calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "data": data,
                "timeout_secs": timeout_secs,
                "prefer_api_key": prefer_api_key,
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
    def __init__(self, text="", status=200, url="https://dummy.example/x", headers=None):
        self._text = text
        self.status = status
        self.url = url
        self.headers = headers or {}

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

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def request(self, method, url, params=None, data=None, max_redirects=None):
            captured["requests"].append(
                {"method": method, "url": url, "params": params, "data": data, "max_redirects": max_redirects}
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

    session_kwargs = captured["sessions"][0]
    assert session_kwargs["headers"]["Authorization"] == "secret-api-key"
    assert session_kwargs["cookies"] == {}


async def test_request_without_api_key_uses_session_cookie(api, monkeypatch):
    captured = install_fake_aiohttp(monkeypatch, [FakeAiohttpResponse(text="ok")])
    api._authenticated = True
    api.api_key = ""

    await api._request("GET", "https://dummy.example/ajax.php", prefer_api_key=True)

    session_kwargs = captured["sessions"][0]
    assert session_kwargs["cookies"] == {"session": "test-cookie"}
    assert "Authorization" not in session_kwargs["headers"]


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


async def test_site_page_upload_redirect_loop_raises_login_error(api, monkeypatch):
    # Expired/invalid cookies make the site redirect to login until aiohttp
    # gives up; that must surface as LoginError.
    install_fake_aiohttp(monkeypatch, [aiohttp.TooManyRedirects(cast("Any", None), ())])
    api._authenticated = True
    api.passkey = "PK"

    with pytest.raises(LoginError):
        await api.site_page_upload({}, UploadFiles(torrent_data=b"torrent"))


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
