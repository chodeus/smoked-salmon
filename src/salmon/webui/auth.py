"""App-level token authentication for the web interface (LAN deployments).

The web UI holds the account's tracker cookies and can trigger real uploads and
deletes, so on any non-loopback bind it must sit behind a shared secret. The
token comes from the SALMON_WEB_TOKEN env var (so a container can inject it) or
``[upload.web_interface] auth_token`` in config. When no token is configured the
gate is a no-op — intended only for a loopback/dev bind.
"""

import hmac
import os

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE_NAME = "salmon_web_token"
ENV_VAR = "SALMON_WEB_TOKEN"
# Reachable without a token: the login form's own calls and the health probe.
EXEMPT_API_PATHS = {"/api/login", "/api/auth", "/api/health"}


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
    """Constant-time token comparison; False if either side is missing."""
    return bool(supplied) and bool(expected) and hmac.compare_digest(supplied, expected)


class AuthMiddleware:
    """Require the web token on /api routes and the websocket when one is set."""

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.token is None or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        if not path.startswith("/api/") or path in EXEMPT_API_PATHS or method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        if token_matches(_token_from_scope(scope), self.token):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await receive()  # consume websocket.connect, then reject the handshake
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]}
        )
        await send({"type": "http.response.body", "body": b'{"detail":"Authentication required."}'})


auth_router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    token: str


@auth_router.get("/auth")
def auth_status(request: Request) -> dict:
    """Report whether a token is required and whether this caller is authenticated."""
    token = getattr(request.app.state, "auth_token", None)
    if not token:
        return {"required": False, "authenticated": True}
    return {"required": True, "authenticated": token_matches(_token_from_scope(request.scope), token)}


@auth_router.post("/login")
def login(req: LoginRequest, request: Request, response: Response) -> dict:
    """Exchange the token for an httponly session cookie."""
    token = getattr(request.app.state, "auth_token", None)
    if not token:
        return {"required": False, "authenticated": True}
    if not token_matches(req.token, token):
        raise HTTPException(status_code=401, detail="Invalid token.")
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30, path="/"
    )
    return {"required": True, "authenticated": True}
