"""FastAPI application factory for the salmon web interface."""

import logging
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from salmon import cfg
from salmon.trackers import account
from salmon.webui import output
from salmon.webui.auth import Auth, AuthMiddleware, auth_router, resolve_auth_token
from salmon.webui.egress import EgressMiddleware
from salmon.webui.interaction import install_interaction_patches
from salmon.webui.jobs import manager
from salmon.webui.routers import browse, checks, convert, jobs, search, spectrals, system, tools, upload

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@asynccontextmanager
async def _jobs_running(app: FastAPI) -> AsyncIterator[None]:
    """While the server runs: jobs, their output captured till all have ended, and every tracker request here."""
    with ExitStack() as capture:
        capture.enter_context(output.capturing())
        async with account.requests_on_this_loop():
            try:
                yield
            finally:
                # A job ends once the workers it left have. One still running keeps the capture (the process is
                # ending), so what it writes never reaches the terminal: held by the app, never closed.
                if not await manager.stop():
                    app.state.kept_capture = capture.pop_all()


def create_app(dev: bool = False, host: str = "127.0.0.1", auth_token: str | None = None) -> FastAPI:
    install_interaction_patches()
    token = resolve_auth_token(auth_token)
    app = FastAPI(
        title="salmon web",
        # API docs expose the whole surface; only in dev.
        docs_url="/api/docs" if dev else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if dev else None,
        lifespan=_jobs_running,
    )
    app.state.auth_token = token
    app.state.auth = Auth(token) if token else None
    app.state.dev = dev

    # Innermost, so every route's answer passes it.
    app.add_middleware(EgressMiddleware)

    if dev:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Added last, so outermost: CORS answers a preflight itself, which must not come before the Origin check.
    # It checks every request's Host first (DNS rebinding): loopback names, the bind address and allowed_hosts.
    app.add_middleware(
        AuthMiddleware,
        auth=app.state.auth,
        bind_host=host,
        allowed_hosts=list(getattr(cfg.upload.web_interface, "allowed_hosts", []) or []),
        dev=dev,
    )

    for router in (system, search, browse, spectrals, convert, checks, jobs, upload, tools):
        app.include_router(router.router, prefix="/api")
    app.include_router(auth_router, prefix="/api")

    if (STATIC_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    else:
        logger.warning(
            "Web UI assets not found at %s — the browser UI will 404 while /api endpoints work. "
            "Build the frontend (npm run build in webui/) or use the Docker image.",
            STATIC_DIR,
        )

    return app
