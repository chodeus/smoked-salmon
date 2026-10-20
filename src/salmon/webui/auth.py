"""App-level token authentication for the web interface (LAN deployments).

The web UI holds the account's tracker cookies and can trigger real uploads and
deletes, so on any non-loopback bind it must sit behind a shared secret. The
token comes from the SALMON_WEB_TOKEN env var (so a container can inject it) or
``[upload.web_interface] auth_token`` in config; with neither, ``salmon web`` makes one per
run. Every /api request needs it, or a session cookie a login traded it for (upstream's ``Auth``),
and one from another site is refused whatever it carries.
Every request's Host must be a loopback name, the bind address or an allowed host (security.py).
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Iterable

import asyncclick as click
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from salmon.config.validations import WEB_TOKEN_MIN_LENGTH
from salmon.webui.security import host_allowed, origin_allowed

COOKIE_NAME = "salmon_web_session"
# The fork's cookie before sessions held the token itself: login and logout delete it.
OLD_COOKIE_NAME = "salmon_web_token"
ENV_VAR = "SALMON_WEB_TOKEN"
SESSION_MAX_AGE = 30 * 24 * 3600
# Each login adds one; the oldest goes first beyond this.
MAX_SESSIONS = 100
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


def _encode(value: str) -> bytes:
    # Header values can hold surrogates (undecodable bytes); compare_digest raises on non-ASCII str.
    return value.encode("utf-8", "surrogatepass")


def _session_key(session_id: str) -> bytes:
    # Sessions are looked up by a digest of the id, so the lookup's timing says nothing about the ids.
    return hashlib.sha256(_encode(session_id)).digest()


class Auth:
    """The token, and the sessions opened with it. Sessions live in memory: a restart logs every browser out."""

    def __init__(self, token: str) -> None:
        self._token = _encode(token)
        self._sessions: dict[bytes, float] = {}

    def token_matches(self, supplied: str | None) -> bool:
        """Constant-time comparison with the token."""
        if not supplied:
            return False
        return hmac.compare_digest(_encode(supplied), self._token)

    def open_session(self) -> str:
        """A new session id for the login cookie."""
        self._prune()
        while len(self._sessions) >= MAX_SESSIONS:
            del self._sessions[next(iter(self._sessions))]
        session_id = secrets.token_urlsafe(32)
        self._sessions[_session_key(session_id)] = time.monotonic() + SESSION_MAX_AGE
        return session_id

    def session_valid(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        expires = self._sessions.get(_session_key(session_id))
        return expires is not None and expires > time.monotonic()

    def close_session(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(_session_key(session_id), None)

    def authenticated(self, authorization: str | None, session_id: str | None) -> bool:
        """Whether a request carries the token as ``Authorization: Bearer`` or a valid session cookie."""
        if authorization:
            scheme, _, credentials = authorization.partition(" ")
            if scheme.lower() == "bearer" and self.token_matches(credentials.strip()):
                return True
        return self.session_valid(session_id)

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [key for key, expires in self._sessions.items() if expires <= now]:
            del self._sessions[key]


def _cookie(headers: dict[str, str], name: str) -> str | None:
    for part in headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _sent_as_json(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


class AuthMiddleware:
    """Refuse an unknown Host on every request, then another site's /api requests, then any without the web token."""

    def __init__(
        self,
        app: ASGIApp,
        auth: Auth | None,
        *,
        bind_host: str = "127.0.0.1",
        allowed_hosts: Iterable[str] = (),
        dev: bool = False,
    ):
        self.app = app
        self.auth = auth
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
        if self.auth is None:
            await _refuse(scope, receive, send, 503, "No web token is set.")
            return
        if self.auth.authenticated(headers.get("authorization"), _cookie(headers, COOKIE_NAME)):
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


def _auth(connection: HTTPConnection) -> Auth | None:
    return getattr(connection.app.state, "auth", None)


def _session_cookie(connection: HTTPConnection) -> str | None:
    # The middleware's own parser: with two cookies of the name, every check reads the same one.
    return _cookie({key.lower(): value for key, value in connection.headers.items()}, COOKIE_NAME)


def still_authenticated(connection: HTTPConnection) -> bool:
    """Whether what a request or websocket logged in with still does: a logout ends its session."""
    auth = _auth(connection)
    return auth is not None and auth.authenticated(connection.headers.get("authorization"), _session_cookie(connection))


# The routes are coroutines, so they run on the event loop one at a time: Auth is not thread-safe.
@auth_router.get("/auth")
async def auth_status(request: Request) -> dict:
    """Report whether a token is required and whether this caller is authenticated."""
    return {"required": True, "authenticated": still_authenticated(request)}


@auth_router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    """Exchange the token for an httponly cookie holding a new session id."""
    auth = _auth(request)
    if auth is None:
        raise HTTPException(status_code=503, detail="No web token is set.")
    if not auth.token_matches(req.token):
        raise HTTPException(status_code=401, detail="Invalid token.")
    response.set_cookie(
        COOKIE_NAME,
        auth.open_session(),
        httponly=True,
        samesite="strict",
        max_age=SESSION_MAX_AGE,
        path="/",
        secure=_secure(request),
    )
    response.delete_cookie(OLD_COOKIE_NAME, path="/")
    return {"required": True, "authenticated": True}


@auth_router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    """End this browser's session; the middleware has already refused one that is not logged in.

    A caller logged in by a Bearer token stays so: the answer says whether it still is.
    """
    auth = _auth(request)
    if auth is not None:
        auth.close_session(_session_cookie(request))
    secure = _secure(request)
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=secure)
    response.delete_cookie(OLD_COOKIE_NAME, path="/")
    return {"required": True, "authenticated": still_authenticated(request)}


def _secure(request: Request) -> bool:
    # Behind a TLS reverse proxy the request arrives as http. Trusting the header here can only add Secure to the
    # cookie, which keeps it off plain http: a client that forges it only breaks its own login.
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"
