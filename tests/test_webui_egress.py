"""salmon web sends the browser no secret it knows, and sends an image host nothing but images."""

import io
import json
import os
import time
from collections.abc import Iterator
from urllib.parse import quote, unquote

import asyncclick as click
import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from webui_client import web_client  # pyright: ignore[reportMissingImports]

from salmon import cfg
from salmon.config.validations import Seedbox
from salmon.trackers import base as tracker_base
from salmon.webui import egress
from salmon.webui.egress import EgressMiddleware, config_secrets
from salmon.webui.jobs import FINISHED, manager
from salmon.webui.routers import tools

SESSION = "sess-0123456789/abcdef+ghij=="
API_KEY = "test-api-key-0123456789"
KEEPLOGGED = "test-keeplogged-0123"
BOX_PASSWORD = "box-pass-0123"
PASSKEY = "testpasskey0123456789abcdef0123"
DIGITS = "12345678"
RCLONE_PASS = "rclone-pass-0123"
AI_PASSWORD = "ai-pass-0123"
USERHASH = "test-userhash-0123"


@pytest.fixture
def secrets_set(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Plant fake secrets where the config and a tracker answer keep them; returns each form a leak could take."""
    assert cfg.tracker.red is not None
    monkeypatch.setattr(cfg.tracker.red, "session", SESSION)
    monkeypatch.setattr(cfg.tracker.red, "api_key", API_KEY)
    monkeypatch.setattr(cfg.tracker.red, "keeplogged", KEEPLOGGED)
    monkeypatch.setattr(cfg.image, "catbox_userhash", USERHASH)
    box = Seedbox(
        name="box", torrent_client=f"http://user:{BOX_PASSWORD}@127.0.0.1:8080", extra_args=["--sftp-pass", RCLONE_PASS]
    )
    monkeypatch.setattr(cfg, "seedbox", [box])
    monkeypatch.setattr(cfg.upload.ai_review, "base_url", f"https://user:{AI_PASSWORD}@ai.example/v1")
    monkeypatch.setattr(tracker_base, "_learned_secrets", {PASSKEY})
    egress._configured_secrets.cache_clear()
    configured = [SESSION, unquote(SESSION), quote(SESSION, safe=""), API_KEY, KEEPLOGGED, USERHASH]
    yield [*configured, BOX_PASSWORD, RCLONE_PASS, AI_PASSWORD, PASSKEY]
    egress._configured_secrets.cache_clear()


def test_the_configs_credentials_are_collected_but_not_its_placeholders(secrets_set, monkeypatch):
    monkeypatch.setattr(cfg.metadata.qobuz, "user_auth_token", "user_auth_token")

    found = config_secrets(cfg)

    assert set(secrets_set) - {PASSKEY} <= set(found)
    assert "user_auth_token" not in found


def _wait(client, job_id: str) -> dict:
    for _ in range(300):
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in FINISHED:
            return body
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_a_jobs_log_and_error_reach_the_browser_without_a_secret(secrets_set):
    async def leak(_job) -> dict:
        click.echo(f"cookie {SESSION} as sent {quote(SESSION, safe='')} key {API_KEY} keeplogged {KEEPLOGGED}")
        click.echo(f"seedbox http://user:{BOX_PASSWORD}@127.0.0.1:8080 announce .../{PASSKEY}/announce")
        click.echo(f"rclone --sftp-pass {RCLONE_PASS}; review endpoint failed with {AI_PASSWORD}")
        raise RuntimeError(f"GET https://tracker.example/torrents.php?action=download&torrent_pass={PASSKEY}")

    with web_client() as client:
        # On the server's loop, as a route creates it.
        assert client.portal is not None
        job = client.portal.call(lambda: manager.create_threaded("leak", "Leak", leak))
        finished = _wait(client, job.id)
        listed = client.get("/api/jobs").text
        one = client.get(f"/api/jobs/{job.id}").text

    assert finished["status"] == "failed"
    assert finished["log"], "the job wrote its lines"
    for secret in secrets_set:
        assert secret not in listed
        assert secret not in one
    assert "[REDACTED]" in one


def _app() -> Starlette:
    async def numbers(_request) -> JSONResponse:
        return JSONResponse({"size": int(DIGITS), "note": f"password {DIGITS}", "key": API_KEY})

    async def text(_request) -> PlainTextResponse:
        return PlainTextResponse(f"failed with {API_KEY}")

    async def events(websocket) -> None:
        await websocket.accept()
        await websocket.send_json({"line": f"key {API_KEY}", "size": int(DIGITS)})
        await websocket.close()

    routes = [Route("/api/numbers", numbers), Route("/api/text", text), WebSocketRoute("/api/ws", events)]
    return Starlette(routes=routes, middleware=[Middleware(EgressMiddleware)])


def test_a_secret_of_digits_is_masked_in_strings_and_leaves_numbers_whole(monkeypatch):
    monkeypatch.setattr(egress, "_configured_secrets", lambda: (API_KEY, DIGITS))

    with TestClient(_app()) as client:
        body = client.get("/api/numbers")
        text = client.get("/api/text")
        with client.websocket_connect("/api/ws") as websocket:
            event = websocket.receive_text()

    assert body.json() == {"size": int(DIGITS), "note": "password [REDACTED]", "key": "[REDACTED]"}
    assert int(body.headers["content-length"]) == len(body.content)
    assert text.text == "failed with [REDACTED]"
    assert json.loads(event) == {"line": "key [REDACTED]", "size": int(DIGITS)}


def test_the_web_token_is_masked_with_the_other_secrets(monkeypatch):
    monkeypatch.setattr(egress, "_configured_secrets", lambda: (API_KEY,))
    token = "fake-web-token-0123456789abcdef"

    async def text(_request) -> PlainTextResponse:
        return PlainTextResponse(f"{token} {API_KEY}")

    app = Starlette(routes=[Route("/api/text", text)], middleware=[Middleware(EgressMiddleware)])
    app.state.auth_token = token
    with TestClient(app) as client:
        shown = client.get("/api/text").text

    assert shown == "[REDACTED] [REDACTED]"


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (2, 2), 7).save(buffer, "PNG")
    return buffer.getvalue()


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.effect_noise((64, 64), 40).convert("RGB").save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def album() -> str:
    path = os.path.join(os.path.realpath(cfg.directory.download_directory), "egress-album")
    os.makedirs(path, exist_ok=True)
    return path


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("cover.jpg", b"d8:announce42:https://tracker.example/" + PASSKEY.encode() + b"/announcee"),
        ("cover.png", _png()[:40]),
        ("cover.jpg", _jpeg()[:-200]),
        ("cover.png", b""),
    ],
    ids=["torrent-named-as-an-image", "cut-short", "jpeg-cut-short", "empty"],
)
def test_the_image_upload_sends_a_host_nothing_but_whole_images(album, monkeypatch, name, content):
    sent: list[list[str]] = []

    async def upload_images(paths, _host) -> list[str]:
        sent.append(paths)
        return ["https://images.example/x.png" for _ in paths]

    monkeypatch.setattr(tools, "upload_images", upload_images)
    refused, image = os.path.join(album, name), os.path.join(album, "real.png")
    with open(refused, "wb") as handle:
        handle.write(content)
    with open(image, "wb") as handle:
        handle.write(_png())

    with web_client() as client:
        refusal = client.post("/api/images/upload", json={"paths": [image, refused], "host": "catbox"})
        accepted = client.post("/api/images/upload", json={"paths": [image], "host": "catbox"})

    assert refusal.status_code == 422
    assert f"Not uploading {refused}" in refusal.json()["detail"]
    assert accepted.status_code == 200
    assert sent == [[image]]


async def test_an_accounts_authkey_and_passkey_are_remembered_for_salmon_web(monkeypatch):
    from salmon.trackers.red import RedApi

    api = RedApi()

    async def index(_action, params=None) -> dict:
        return {"authkey": "test-authkey-0123456789", "passkey": PASSKEY}

    monkeypatch.setattr(api, "api_call", index)
    monkeypatch.setattr(tracker_base, "_learned_secrets", set())

    await api.authenticate()

    assert sorted(tracker_base.learned_secrets()) == sorted(["test-authkey-0123456789", PASSKEY])
