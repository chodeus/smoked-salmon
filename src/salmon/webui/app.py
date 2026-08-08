"""FastAPI application factory for the salmon web interface."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from salmon.webui.routers import browse, checks, convert, jobs, search, spectrals, system

STATIC_DIR = Path(__file__).parent / "static"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def create_app(dev: bool = False, host: str = "127.0.0.1") -> FastAPI:
    app = FastAPI(title="salmon web", docs_url="/api/docs", openapi_url="/api/openapi.json")

    if host in LOOPBACK_HOSTS:
        # DNS-rebinding protection; skipped for non-loopback binds (seedbox use)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    if dev:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    for router in (system, search, browse, spectrals, convert, checks, jobs):
        app.include_router(router.router, prefix="/api")

    if (STATIC_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
