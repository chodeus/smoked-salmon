"""FastAPI application factory for the salmon web interface."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from salmon import cfg
from salmon.webui.auth import AuthMiddleware, auth_router, resolve_auth_token
from salmon.webui.interaction import install_interaction_patches
from salmon.webui.routers import browse, checks, convert, jobs, search, spectrals, system, upload

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def create_app(dev: bool = False, host: str = "127.0.0.1", auth_token: str | None = None) -> FastAPI:
    install_interaction_patches()
    token = resolve_auth_token(auth_token)
    app = FastAPI(
        title="salmon web",
        # API docs expose the whole surface; only in dev.
        docs_url="/api/docs" if dev else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if dev else None,
    )
    app.state.auth_token = token
    app.state.dev = dev

    configured_hosts = list(getattr(cfg.upload.web_interface, "allowed_hosts", []) or [])
    if host in LOOPBACK_HOSTS:
        # DNS-rebinding protection for loopback/dev binds.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", *configured_hosts])
    elif configured_hosts:
        # A LAN bind can be locked to its own hostname(s)/IP via config.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=configured_hosts)

    # The token gate is the primary protection for a non-loopback bind.
    app.add_middleware(AuthMiddleware, token=token)

    if dev:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    for router in (system, search, browse, spectrals, convert, checks, jobs, upload):
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
