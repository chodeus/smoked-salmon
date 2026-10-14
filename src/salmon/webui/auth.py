"""App-level token authentication for the web interface (LAN deployments).

The web UI holds the account's tracker cookies and can trigger real uploads and
deletes, so on any non-loopback bind it must sit behind a shared secret. The
token comes from the SALMON_WEB_TOKEN env var (so a container can inject it) or
``[upload.web_interface] auth_token`` in config; with neither, ``salmon web`` makes one per
run. Every /api request needs it, and one from another site is refused whatever it carries.
"""

import hmac
import json
import os
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE_NAME = "salmon_web_token"
ENV_VAR = "SALMON_WEB_TOKEN"
# Reachable without a token: the login form's own calls and the health probe.
# /api/health exposes config, directories, binary paths and trackers, so it requires auth.
EXEMPT_API_PATHS = {"/api/login", "/api/auth"}
# Methods that change nothing. Any other must come as JSON, which another site's page cannot send unpreflighted.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def resolve_auth_token(explicit: str | None = None) -> str | None:
    """Return the configured web token (explicit arg, then env, then config); '' -> None."""
    if explicit:
        return explicit
    env = os.environ.get(ENV_VAR)
    if env:
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


def _host_only(host_header: str) -> str:
    """Hostname from a Host header, dropping the port and IPv6 brackets."""
    value = host_header.strip().lower()
    if value.startswith("["):  # [::1] or [::1]:port
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if ":" in value else value


def origin_allowed(origin: str | None, host_header: str | None) -> bool:
    """Whether a request's Origin is this server's own host (any port, for the Vite dev server); no Origin passes."""
    if not origin:
        return True  # non-browser clients (curl, scripts) send no Origin
    origin_host = urlparse(origin).hostname
    if origin_host is None or not host_header:
        return False
    return origin_host.lower() == _host_only(host_header)


def _sent_as_json(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


class AuthMiddleware:
    """Require the web token on /api routes and the websocket, refusing another site's requests first."""

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = scope.get("method", "")
        is_http = scope["type"] == "http"
        if not origin_allowed(headers.get("origin"), headers.get("host")):
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
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30, path="/")
    return {"required": True, "authenticated": True}
