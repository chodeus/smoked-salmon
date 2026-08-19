"""App-level token auth and same-host websocket origin checks."""

import contextlib

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from salmon.webui.app import create_app
from salmon.webui.jobs import manager


def _clean():
    manager.jobs.clear()
    manager._subscribers.clear()
    manager._active_lock_keys.clear()


def test_no_token_configured_leaves_api_open():
    _clean()
    with TestClient(create_app(auth_token=None), base_url="http://localhost") as c:
        assert c.get("/api/jobs").status_code == 200


def test_token_required_rejects_unauthenticated():
    _clean()
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        assert c.get("/api/jobs").status_code == 401
        # exempt endpoints stay reachable so the login screen can load
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/auth").json() == {"required": True, "authenticated": False}


def test_bearer_header_authenticates():
    _clean()
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        assert c.get("/api/jobs", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_login_sets_cookie_then_requests_succeed():
    _clean()
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        assert c.post("/api/login", json={"token": "nope"}).status_code == 401
        ok = c.post("/api/login", json={"token": "s3cret"})
        assert ok.status_code == 200 and ok.json()["authenticated"] is True
        # TestClient keeps the cookie jar; subsequent calls are now authorized
        assert c.get("/api/jobs").status_code == 200
        assert c.get("/api/auth").json()["authenticated"] is True


def test_docs_hidden_unless_dev():
    _clean()
    with TestClient(create_app(auth_token=None), base_url="http://localhost") as c:
        assert c.get("/api/openapi.json").status_code == 404
    with TestClient(create_app(dev=True), base_url="http://localhost") as c:
        assert c.get("/api/openapi.json").status_code == 200


def test_debug_threads_dev_only():
    _clean()
    with TestClient(create_app(auth_token=None), base_url="http://localhost") as c:
        assert c.get("/api/debug/threads").status_code == 404
    with TestClient(create_app(dev=True), base_url="http://localhost") as c:
        assert c.get("/api/debug/threads").status_code == 200


def test_ws_rejects_without_token_but_accepts_with_cookie():
    _clean()
    with TestClient(create_app(auth_token="s3cret"), base_url="http://localhost") as c:
        rejected = False
        try:
            with c.websocket_connect("/api/ws", headers={"host": "localhost"}) as ws:
                ws.receive_text()
        except WebSocketDisconnect:
            rejected = True
        assert rejected, "ws without a token should be rejected"
        # A browser sends the cookie automatically on a same-origin ws; TestClient needs it explicit.
        with c.websocket_connect(
            "/api/ws", headers={"host": "localhost", "cookie": "salmon_web_token=s3cret"}
        ):
            pass  # handshake accepted with the cookie


def test_ws_same_host_lan_origin_accepted():
    _clean()
    with TestClient(create_app(host="0.0.0.0"), base_url="http://10.0.20.11:55155") as c:
        headers = {"host": "10.0.20.11:55155", "origin": "http://10.0.20.11:55155"}
        with c.websocket_connect("/api/ws", headers=headers):
            pass  # a LAN browser reaching the box at its own IP is same-origin
    _clean()


def test_ws_cross_site_origin_rejected_on_lan_bind():
    _clean()
    with TestClient(create_app(host="0.0.0.0"), base_url="http://10.0.20.11:55155") as c:
        headers = {"host": "10.0.20.11:55155", "origin": "http://evil.example.com"}
        with contextlib.suppress(WebSocketDisconnect):  # noqa: SIM117
            with c.websocket_connect("/api/ws", headers=headers) as ws:
                ws.receive_text()
                raise AssertionError("cross-site origin should be rejected")
    _clean()
