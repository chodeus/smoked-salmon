"""App-level token authentication for the web interface (LAN deployments).

The web UI holds the account's tracker cookies and can trigger real uploads and
deletes, so on any non-loopback bind it must sit behind a shared secret. The
token comes from the SALMON_WEB_TOKEN env var (so a container can inject it) or
``[upload.web_interface] auth_token`` in config; with neither, ``salmon web`` makes one per
run. Every /api request needs it, and one from another site is refused whatever it carries.
Every request's Host must be a loopback name, the bind address or an allowed host (security.py).
"""

import hmac
import json
import os
from collections.abc import Iterable

import asyncclick as click
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from salmon.config.validations import WEB_TOKEN_MIN_LENGTH
from salmon.webui.security import host_allowed, origin_allowed

COOKIE_NAME = "salmon_web_token"
ENV_VAR = "SALMON_WEB_TOKEN"
# Reachable without a token: the login form's own calls and the health probe.
# /api/health exposes config, directories, binary paths and trackers, so it requires auth.
EXEMPT_API_PATHS = {"/api/login", "/api/auth"}
# Methods that change nothing. Any other must come as JSON, which another site's page cannot send unpreflighted.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def resolve_auth_token(explicit: str | None = None) -> str | None:
    """Return the configured web token (explicit arg, then env, then config); '' -> None.

    Raises:
        click.ClickException: SALMON_WEB_TOKEN is too short.
    """
    if explicit:
        return explicit
    env = os.environ.get(ENV_VAR)
    if env:
        if len(env) < WEB_TOKEN_MIN_LENGTH:
            raise click.ClickException(f"{ENV_VAR} must be at least {WEB_TOKEN_MIN_LENGTH} characters.")
        return env
    from salmon import cfg

    return getattr(cfg.upload.web_interface, "auth_token", None) or None


def _token_from_scope(scope: Scope) -> str | None:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return None


def token_matches(supplied: str | None, expected: str | None) -> bool:
    """Constant-time token comparison; False if either side is missing.

    Compare as bytes: a latin-1-decoded header can hold non-ASCII, which makes
    hmac.compare_digest raise TypeError on str operands (an uncaught 500).
    """
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass"))


def _sent_as_json(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


class AuthMiddleware:
    """Refuse an unknown Host on every request, then another site's /api requests, then any without the web token."""

    def __init__(
        self,
        app: ASGIApp,
        token: str | None,
        *,
        bind_host: str = "127.0.0.1",
        allowed_hosts: Iterable[str] = (),
        dev: bool = False,
    ):
        self.app = app
        self.token = token
        self.bind_host = bind_host
        self.allowed_hosts = list(allowed_hosts)
        self.dev = dev

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            send = _with_security_headers(send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if not host_allowed(headers.get("host"), self.bind_host, self.allowed_hosts):
            await _refuse(
                scope,
                receive,
                send,
                403,
                "This host name is not allowed. Add it to allowed_hosts in the [upload.web_interface] config section.",
            )
            return
        if not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        is_http = scope["type"] == "http"
        if not origin_allowed(headers.get("origin"), headers.get("host"), self.dev):
            await _refuse(scope, receive, send, 403, "Requests from another site are refused.")
            return
        if is_http and method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        if is_http and method not in SAFE_METHODS and not _sent_as_json(headers.get("content-type", "")):
            await _refuse(scope, receive, send, 415, "Send this request as JSON.")
            return
        if scope["path"] in EXEMPT_API_PATHS:
            await self.app(scope, receive, send)
            return
        if not self.token:
            await _refuse(scope, receive, send, 503, "No web token is set.")
            return
        if token_matches(_token_from_scope(scope), self.token):
            await self.app(scope, receive, send)
            return
        await _refuse(scope, receive, send, 401, "Authentication required.")


def _with_security_headers(send: Send) -> Send:
    """``send``, adding upstream's nosniff and no-framing headers to every response."""

    async def sending(message: Message) -> None:
        if message["type"] == "http.response.start":
            extra = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY")]
            message = {**message, "headers": [*message.get("headers", []), *extra]}
        await send(message)

    return sending


async def _refuse(scope: Scope, receive: Receive, send: Send, status: int, detail: str) -> None:
    if scope["type"] == "websocket":
        await receive()  # consume websocket.connect, then reject the handshake
        await send({"type": "websocket.close", "code": 1008})
        return
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps({"detail": detail}).encode()})


auth_router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    token: str


@auth_router.get("/auth")
def auth_status(request: Request) -> dict:
    """Report whether a token is required and whether this caller is authenticated."""
    token = getattr(request.app.state, "auth_token", None)
    return {"required": True, "authenticated": token_matches(_token_from_scope(request.scope), token)}


@auth_router.post("/login")
def login(req: LoginRequest, request: Request, response: Response) -> dict:
    """Exchange the token for an httponly session cookie."""
    token = getattr(request.app.state, "auth_token", None)
    if not token:
        raise HTTPException(status_code=503, detail="No web token is set.")
    if not token_matches(req.token, token):
        raise HTTPException(status_code=401, detail="Invalid token.")
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 24 * 30,
        path="/",
        secure=_secure(request),
    )
    return {"required": True, "authenticated": True}


def _secure(request: Request) -> bool:
    # Behind a TLS reverse proxy the request arrives as http. Trusting the header here can only add Secure to the
    # cookie, which keeps it off plain http: a client that forges it only breaks its own login.
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"
