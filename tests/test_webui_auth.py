"""App-level token auth, and the same-host and JSON rules every /api request and the websocket meet first."""

import re

import pytest
from asyncclick.testing import CliRunner
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from webui_client import WEB_TOKEN, web_client  # pyright: ignore[reportMissingImports]

from salmon import cfg
from salmon.webui import web
from salmon.webui.app import create_app
from salmon.webui.jobs import manager


def _no_configured_token(monkeypatch) -> None:
    """Neither token source set, whatever the machine running the tests has configured."""
    monkeypatch.delenv("SALMON_WEB_TOKEN", raising=False)
    monkeypatch.setattr(cfg.upload.web_interface, "auth_token", None)


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
        same_host = c.request(method, "/api/jobs/job-0001/cancel", headers={"Origin": "http://localhost:55155"})
    assert refused.status_code == 403
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
        with c.websocket_connect("/api/ws", headers={"host": "localhost", "cookie": "salmon_web_token=s3cret"}):
            pass  # handshake accepted with the cookie


def test_ws_same_host_lan_origin_accepted():
    _clean()
    with web_client(base_url="http://192.168.1.20:55155", host="0.0.0.0") as c:
        headers = {
            "host": "192.168.1.20:55155",
            "origin": "http://192.168.1.20:55155",
            "cookie": f"salmon_web_token={WEB_TOKEN}",
        }
        with c.websocket_connect("/api/ws", headers=headers):
            pass  # a LAN browser reaching the box at its own IP is same-origin
    _clean()


def test_ws_cross_site_origin_rejected_on_lan_bind():
    _clean()
    with web_client(base_url="http://192.168.1.20:55155", host="0.0.0.0") as c:
        headers = {
            "host": "192.168.1.20:55155",
            "origin": "http://evil.example.com",
            "cookie": f"salmon_web_token={WEB_TOKEN}",
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
    monkeypatch.setenv("SALMON_WEB_TOKEN", "configured-web-token-0001")

    result = await CliRunner().invoke(web, [])

    assert result.exit_code == 0, result.output
    assert "configured-web-token-0001" not in result.output
    assert "#token=" not in result.output
    assert serve_nothing[0].state.auth_token == "configured-web-token-0001"


async def test_a_lan_bind_without_a_token_does_not_start(serve_nothing):
    result = await CliRunner().invoke(web, ["--host", "0.0.0.0"])

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


def test_a_dev_preflight_from_another_host_is_refused_before_cors_answers_it():
    _clean()
    with web_client(dev=True) as c:
        preflight = c.options(
            "/api/jobs",
            headers={
                "Host": "127.0.0.1:55155",
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert preflight.status_code == 403
