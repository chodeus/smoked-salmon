"""App-level token auth, and the same-host and JSON rules every /api request and the websocket meet first."""

import re

import pytest
from asyncclick.testing import CliRunner
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from webui_client import WEB_TOKEN, web_client  # pyright: ignore[reportMissingImports]

from salmon import cfg
from salmon.config.validations import UploadWebInterface
from salmon.webui import web
from salmon.webui.app import create_app
from salmon.webui.auth import COOKIE_NAME, OLD_COOKIE_NAME
from salmon.webui.jobs import manager

# The bind address under test: nothing is served on it.
LAN_BIND = "0.0.0.0"  # nosec B104


def _no_configured_token(monkeypatch) -> None:
    """Neither token source set, whatever the machine running the tests has configured."""
    monkeypatch.delenv("SALMON_WEB_TOKEN", raising=False)
    monkeypatch.setattr(cfg.upload.web_interface, "auth_token", None)


def _session(c: TestClient, token: str = WEB_TOKEN) -> str:
    """Log in, and return the session cookie as a header value (TestClient's websocket needs it explicit)."""
    answer = c.post("/api/login", json={"token": token})
    assert answer.status_code == 200, answer.text
    return f"{COOKIE_NAME}={answer.cookies[COOKIE_NAME]}"


def _clean():
    manager.jobs.clear()
    manager._subscribers.clear()
    manager._active_lock_keys.clear()


def test_an_app_without_a_token_refuses_every_api_request(monkeypatch):
    _clean()
    _no_configured_token(monkeypatch)
    with TestClient(create_app(auth_token=None), base_url="http://localhost") as c:
        answer = c.get("/api/jobs")
        assert answer.status_code == 503
        answer = c.post("/api/login", json={"token": ""})
        assert answer.status_code == 503
        answer = c.get("/api/auth")
        assert answer.json() == {"required": True, "authenticated": False}


def test_token_required_rejects_unauthenticated():
    _clean()
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        answer = c.get("/api/jobs")
        assert answer.status_code == 401
        # /api/health now requires auth (it exposes config, directories, binaries, trackers)
        answer = c.get("/api/health")
        assert answer.status_code == 401
        # /api/auth stays exempt so the login screen can check status
        answer = c.get("/api/auth")
        assert answer.json() == {"required": True, "authenticated": False}


def test_bearer_header_authenticates():
    _clean()
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        answer = c.get("/api/jobs", headers={"Authorization": "Bearer s3cret"})
        assert answer.status_code == 200
        answer = c.get("/api/jobs", headers={"Authorization": "Bearer wrong"})
        assert answer.status_code == 401


def test_login_sets_cookie_then_requests_succeed():
    _clean()
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        answer = c.post("/api/login", json={"token": "nope"})
        assert answer.status_code == 401
        ok = c.post("/api/login", json={"token": "s3cret"})
        assert ok.status_code == 200 and ok.json()["authenticated"] is True
        # TestClient keeps the cookie jar; subsequent calls are now authorized
        answer = c.get("/api/jobs")
        assert answer.status_code == 200
        answer = c.get("/api/auth")
        assert answer.json()["authenticated"] is True


def test_docs_hidden_unless_dev():
    _clean()
    with web_client() as c:
        answer = c.get("/api/openapi.json")
        assert answer.status_code == 404
    with web_client(dev=True) as c:
        answer = c.get("/api/openapi.json")
        assert answer.status_code == 200


def test_debug_threads_dev_only():
    _clean()
    with web_client() as c:
        answer = c.get("/api/debug/threads")
        assert answer.status_code == 404
    with web_client(dev=True) as c:
        answer = c.get("/api/debug/threads")
        assert answer.status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_request_from_another_site_is_refused_with_the_token(method: str):
    _clean()
    with web_client() as c:
        refused = c.request(method, "/api/jobs/job-0001/cancel", headers={"Origin": "http://evil.example.com"})
        other_port = c.request(method, "/api/jobs/job-0001/cancel", headers={"Origin": "http://localhost:8080"})
        same_host = c.request(method, "/api/jobs/job-0001/cancel", headers={"Origin": "http://localhost"})
    assert refused.status_code == 403
    # The port counts: another server on this machine is another site.
    assert other_port.status_code == 403
    # The same host reaches the route: a GET to a POST route, or an unknown job.
    assert same_host.status_code in (405, 409)


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
)
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_a_change_sent_other_than_as_json_is_refused(method: str, content_type: str | None):
    _clean()
    with web_client() as c:
        c.headers.pop("Content-Type")
        headers = {"Content-Type": content_type} if content_type else {}
        resp = c.request(method, "/api/jobs/job-0001/cancel", headers=headers)
        as_json = c.request(
            method, "/api/jobs/job-0001/cancel", headers={"Content-Type": "application/json; charset=utf-8"}
        )
    assert resp.status_code == 415
    assert as_json.status_code in (405, 409)


def test_ws_rejects_without_token_but_accepts_with_cookie():
    _clean()
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        # A refused handshake raises on connect; an accepted one would leave the block quietly.
        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/api/ws", headers={"host": "localhost"}):
            pass
        # A browser sends the cookie automatically on a same-origin ws; TestClient needs it explicit.
        with c.websocket_connect("/api/ws", headers={"host": "localhost", "cookie": _session(c, "s3cret")}):
            pass  # handshake accepted with the session cookie


def test_ws_same_host_lan_origin_accepted():
    _clean()
    with web_client(base_url="http://192.168.1.20:55155", host="192.168.1.20") as c:
        headers = {
            "host": "192.168.1.20:55155",
            "origin": "http://192.168.1.20:55155",
            "cookie": _session(c),
        }
        with c.websocket_connect("/api/ws", headers=headers):
            pass  # a LAN browser reaching the box at its own IP is same-origin
    _clean()


def test_ws_cross_site_origin_rejected_on_lan_bind():
    _clean()
    with web_client(base_url="http://192.168.1.20:55155", host="192.168.1.20") as c:
        headers = {
            "host": "192.168.1.20:55155",
            "origin": "http://evil.example.com",
            "cookie": _session(c),
        }
        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/api/ws", headers=headers):
            pass
    _clean()


@pytest.fixture
def serve_nothing(monkeypatch: pytest.MonkeyPatch) -> list:
    """Keep salmon web from serving, and hand over the app it would have served."""
    import uvicorn

    apps: list = []

    async def serve(self) -> None:
        apps.append(self.config.app)

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    _no_configured_token(monkeypatch)
    return apps


async def test_loopback_without_a_token_makes_one_and_prints_its_login_link(serve_nothing):
    result = await CliRunner().invoke(web, [])

    assert result.exit_code == 0, result.output
    [token] = re.findall(r"http://localhost:55155/#token=([A-Za-z0-9_-]+)", result.output)
    assert len(token) >= 43
    [app] = serve_nothing
    assert app.state.auth_token == token


async def test_a_configured_token_is_never_printed(serve_nothing, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SALMON_WEB_TOKEN", "configured-web-token-0001-0123456789")

    result = await CliRunner().invoke(web, [])

    assert result.exit_code == 0, result.output
    assert "configured-web-token-0001-0123456789" not in result.output
    assert "#token=" not in result.output
    assert serve_nothing[0].state.auth_token == "configured-web-token-0001-0123456789"


async def test_a_lan_bind_without_a_token_does_not_start(serve_nothing):
    result = await CliRunner().invoke(web, ["--host", LAN_BIND])

    assert result.exit_code != 0
    assert "Refusing to serve on 0.0.0.0 without an auth token" in result.output
    assert serve_nothing == []


def test_a_preflight_from_another_site_is_refused():
    _clean()
    with web_client(dev=True) as c:
        refused = c.options("/api/jobs", headers={"Origin": "http://evil.example.com"})
        dev_server = c.options(
            "/api/jobs", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"}
        )
    assert refused.status_code == 403
    assert dev_server.status_code == 200


def test_the_vite_dev_server_passes_only_with_dev():
    # Upstream's rule: with --dev the Vite dev server's own origin passes, whatever host the API is called by.
    _clean()
    headers = {"Host": "127.0.0.1:55155", "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"}
    with web_client(dev=True) as c:
        dev = c.options("/api/jobs", headers=headers)
    with web_client() as c:
        not_dev = c.options("/api/jobs", headers=headers)
    assert dev.status_code == 200
    assert not_dev.status_code == 403


async def test_a_short_environment_token_is_refused(serve_nothing, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SALMON_WEB_TOKEN", "x" * 31)

    result = await CliRunner().invoke(web, [])

    assert result.exit_code != 0
    assert "SALMON_WEB_TOKEN must be at least 32 characters." in result.output
    assert serve_nothing == []


@pytest.mark.parametrize(("token", "kept"), [("", None), ("x" * 32, "x" * 32)])
def test_a_configured_token_of_32_characters_or_none_is_kept(token: str, kept: str | None):
    assert UploadWebInterface(auth_token=token).auth_token == kept


def test_a_short_configured_token_is_refused():
    with pytest.raises(ValueError, match="upload.web_interface.auth_token must be at least 32 characters"):
        UploadWebInterface(auth_token="x" * 31)


@pytest.mark.parametrize("path", ["/", "/api/auth", "/api/jobs"])
def test_an_unknown_host_is_refused_before_anything_else(path: str, monkeypatch: pytest.MonkeyPatch):
    _clean()
    monkeypatch.setattr(cfg.upload.web_interface, "allowed_hosts", [])
    with web_client(base_url="http://evil.example:55155", host=LAN_BIND) as c:
        answer = c.get(path)
    assert answer.status_code == 403
    assert answer.json() == {
        "detail": "This host name is not allowed. Add it to allowed_hosts in the [upload.web_interface] config section."
    }


def test_a_websocket_from_an_unknown_host_is_refused(monkeypatch: pytest.MonkeyPatch):
    _clean()
    monkeypatch.setattr(cfg.upload.web_interface, "allowed_hosts", [])
    with web_client(base_url="http://evil.example:55155", host=LAN_BIND) as c:
        headers = {"host": "evil.example:55155", "cookie": f"{COOKIE_NAME}=any-session"}
        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/api/ws", headers=headers):
            pass


@pytest.mark.parametrize(
    ("bind", "host", "allowed_hosts"),
    [
        (LAN_BIND, "localhost:55155", []),
        (LAN_BIND, "127.0.0.1", []),
        (LAN_BIND, "salmon.lan:55155", ["salmon.lan"]),
        (LAN_BIND, "10.0.0.5", ["10.0.0.5:55155"]),
        ("192.168.1.20", "192.168.1.20:55155", []),
    ],
    ids=["loopback name", "loopback address", "allowed host", "allowed host given a port", "bind address"],
)
def test_loopback_names_the_bind_address_and_allowed_hosts_pass(
    bind: str, host: str, allowed_hosts: list[str], monkeypatch: pytest.MonkeyPatch
):
    _clean()
    monkeypatch.setattr(cfg.upload.web_interface, "allowed_hosts", allowed_hosts)
    with web_client(base_url=f"http://{host}", host=bind) as c:
        answer = c.get("/api/auth")
    assert answer.status_code == 200


def test_every_response_says_nosniff_and_no_framing():
    _clean()
    with web_client() as c:
        answers = [c.get("/api/auth"), c.get("/api/jobs", headers={"Origin": "http://evil.example"})]
    assert [a.status_code for a in answers] == [200, 403]
    for answer in answers:
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert answer.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(("proto", "secure"), [(None, False), ("https", True), ("HTTPS", True), ("http", False)])
def test_the_login_cookie_is_secure_only_behind_tls(proto: str | None, secure: bool):
    _clean()
    headers = {"X-Forwarded-Proto": proto} if proto else {}
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        answer = c.post("/api/login", json={"token": "s3cret"}, headers=headers)
    assert answer.status_code == 200
    assert ("secure" in _set_cookies(answer)[COOKIE_NAME].lower().split("; ")) is secure


def test_a_page_outside_the_api_needs_no_token():
    # The login page itself: 404 where the front end is not built, never 401.
    _clean()
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        answer = c.get("/")
    assert answer.status_code in (200, 404)


async def test_a_wildcard_bind_without_allowed_hosts_says_a_browser_elsewhere_is_refused(
    serve_nothing, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SALMON_WEB_TOKEN", "x" * 32)
    monkeypatch.setattr(cfg.upload.web_interface, "allowed_hosts", [])
    warned = await CliRunner().invoke(web, ["--host", LAN_BIND])
    monkeypatch.setattr(cfg.upload.web_interface, "allowed_hosts", ["salmon.lan"])
    listed = await CliRunner().invoke(web, ["--host", LAN_BIND])
    bound = await CliRunner().invoke(web, ["--host", "192.168.1.20"])

    assert [warned.exit_code, listed.exit_code, bound.exit_code] == [0, 0, 0]
    assert "No allowed_hosts are set" in warned.output
    assert "No allowed_hosts are set" not in listed.output
    assert "No allowed_hosts are set" not in bound.output


def _set_cookies(answer) -> dict[str, str]:
    """Each Set-Cookie header of an answer, by the cookie's name."""
    return {header.split("=", 1)[0]: header for header in answer.headers.get_list("set-cookie")}


def test_login_sets_a_strict_http_only_session_cookie_that_logs_in():
    _clean()
    with TestClient(create_app(auth_token=WEB_TOKEN), base_url="http://localhost") as c:
        answer = c.post("/api/login", json={"token": WEB_TOKEN})
        cookie = _set_cookies(answer)[COOKIE_NAME]
        logged_in = c.get("/api/auth").json()
        jobs = c.get("/api/jobs")
    assert answer.status_code == 200
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert "path=/" in cookie.lower()
    # Plain HTTP: a Secure cookie would never come back.
    assert "secure" not in cookie.lower().split("; ")
    # The cookie is a session, not the token.
    assert WEB_TOKEN not in cookie
    assert logged_in == {"required": True, "authenticated": True}
    assert jobs.status_code == 200


def test_login_deletes_the_cookie_that_held_the_token():
    _clean()
    with TestClient(create_app(auth_token=WEB_TOKEN), base_url="http://localhost") as c:
        answer = c.post("/api/login", json={"token": WEB_TOKEN})
    old = _set_cookies(answer)[OLD_COOKIE_NAME]
    assert "max-age=0" in old.lower()
    assert WEB_TOKEN not in old


@pytest.mark.parametrize("cookie_name", [COOKIE_NAME, OLD_COOKIE_NAME])
def test_the_token_itself_as_a_cookie_is_no_session(cookie_name: str):
    _clean()
    with TestClient(create_app(auth_token=WEB_TOKEN), base_url="http://localhost") as c:
        answer = c.get("/api/jobs", headers={"cookie": f"{cookie_name}={WEB_TOKEN}"})
    assert answer.status_code == 401


def test_logout_ends_the_session():
    _clean()
    with TestClient(create_app(auth_token=WEB_TOKEN), base_url="http://localhost") as c:
        session = _session(c)
        out = c.post("/api/logout", json={})
        after = c.get("/api/auth").json()
        c.cookies.clear()
        # The old cookie, sent again, is no longer a session.
        again = c.get("/api/jobs", headers={"cookie": session})
    assert out.status_code == 200
    assert out.json() == {"required": True, "authenticated": False}
    assert "max-age=0" in _set_cookies(out)[COOKIE_NAME].lower()
    assert after == {"required": True, "authenticated": False}
    assert again.status_code == 401


def test_logout_needs_a_login():
    _clean()
    with TestClient(create_app(auth_token=WEB_TOKEN), base_url="http://localhost") as c:
        answer = c.post("/api/logout", json={})
    assert answer.status_code == 401


def test_a_bearer_token_still_works_without_a_session():
    _clean()
    with web_client() as c:
        answer = c.get("/api/jobs")
    assert answer.status_code == 200
