"""The one filter every /api response body and websocket message passes on its way to the browser."""

import json
import re
from functools import cache
from typing import Any
from urllib.parse import unquote, urlparse

import msgspec
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from salmon.common.redaction import redact_tracker_text
from salmon.config.compat import placeholder
from salmon.trackers.base import learned_secrets, session_cookie_forms
from salmon.uploader.seedbox import seedbox_secrets

# A setting whose name ends so holds a credential, wherever it sits in the config.
_SECRET_SETTING = re.compile(r"(?:^|_)(?:key|token|secret|password|session|keeplogged|userhash)$")


def _url_password(value: str) -> list[str]:
    try:
        password = urlparse(value).password
    except ValueError:
        return []
    return [password, unquote(password)] if password else []


def config_secrets(config: Any) -> list[str]:
    """Every credential the config holds: settings named as one, URL passwords and seedbox secrets, not placeholders."""
    found: list[str] = []

    def walk(node: Any, path: tuple[str, ...] | None) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, None)
        if not isinstance(node, dict):
            return
        for name, value in node.items():
            setting = None if path is None else (*path, name)
            if not isinstance(value, str):
                walk(value, setting)
            # The old template's placeholder for this same setting is no secret.
            elif setting is None or value != placeholder(".".join(setting)):
                if _SECRET_SETTING.search(name):
                    found.extend(session_cookie_forms(value))
                found.extend(_url_password(value))

    walk(msgspec.to_builtins(config), ())
    for seedbox in config.seedbox:
        found.extend(seedbox_secrets(seedbox))
    return list(dict.fromkeys(secret for secret in found if secret))


@cache
def _configured_secrets() -> tuple[str, ...]:
    # The config is read once per process: nothing reloads it.
    from salmon import cfg

    return tuple(config_secrets(cfg))


def known_secrets() -> list[str]:
    """The config's secrets (a web token set there among them) and those the trackers have learned so far."""
    return [*_configured_secrets(), *learned_secrets()]


def masked_text(text: str) -> str:
    """`text` with every known_secrets() value masked, for what the server writes outside /api."""
    return redact_tracker_text(text, known_secrets())


def mask(value: Any, secrets: list[str]) -> Any:
    """`value` with every string in it masked: a string, or the dicts and lists a JSON body or an event holds."""
    if isinstance(value, str):
        return redact_tracker_text(value, secrets)
    if isinstance(value, dict):
        return {mask(key, secrets): mask(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [mask(item, secrets) for item in value]
    return value


def _mask_json(text: str, secrets: list[str]) -> str:
    # Masked as decoded values, never as raw text: a secret of digits would otherwise cut a number out of the JSON.
    try:
        value = json.loads(text)
    except ValueError:
        return redact_tracker_text(text, secrets)
    return json.dumps(mask(value, secrets), ensure_ascii=False, separators=(",", ":"))


def _media_type(start: Message) -> str:
    content_type = dict(start.get("headers", [])).get(b"content-type", b"")
    return content_type.split(b";", 1)[0].strip().lower().decode("latin-1")


class EgressMiddleware:
    """Mask every known secret in each /api JSON or text body and websocket message before it leaves the server."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        def secrets() -> list[str]:
            # Looked up each time: a job's tracker may authenticate at any point.
            token = getattr(getattr(scope.get("app"), "state", None), "auth_token", None)
            return [*known_secrets(), *([token] if token else [])]

        if scope["type"] == "websocket":

            async def send_masked(message: Message) -> None:
                if message["type"] == "websocket.send" and message.get("text") is not None:
                    message = {**message, "text": _mask_json(message["text"], secrets())}
                await send(message)

            await self.app(scope, receive, send_masked)
            return

        start: Message | None = None
        chunks: list[bytes] = []

        async def send_masked_body(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                media_type = _media_type(message)
                if media_type == "application/json" or media_type.startswith("text/"):
                    start = message
                    return
            if start is None or message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = _mask_json(b"".join(chunks).decode("utf-8", errors="replace"), secrets()).encode("utf-8")
            headers = [(name, value) for name, value in start.get("headers", []) if name.lower() != b"content-length"]
            await send({**start, "headers": [*headers, (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, send_masked_body)
