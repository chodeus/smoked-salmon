"""salmon resolves names through the OS (getaddrinfo, never c-ares) and still decodes brotli and zstd answers."""

import importlib.util

import aiohttp.resolver
import anyio
import brotli
import pytest
from aiohttp import web

from salmon.sources import base

try:
    from compression import zstd  # pyright: ignore[reportMissingImports]
except ImportError:  # Python < 3.14
    from backports import zstd

PAYLOAD = b'{"title": "Album"}'


def test_names_resolve_through_the_os() -> None:
    assert aiohttp.resolver.DefaultResolver is aiohttp.resolver.ThreadedResolver
    assert importlib.util.find_spec("aiodns") is None


async def _get_json_encoded(encoding: str, body: bytes) -> dict:
    async def handle(request: web.Request) -> web.Response:
        return web.Response(body=body, content_type="application/json", headers={"Content-Encoding": encoding})

    app = web.Application()
    app.router.add_get("/release/1", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        scraper = base.BaseScraper()
        scraper.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        return await scraper.get_json("/release/1")
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    ("encoding", "body"),
    [("br", brotli.compress(PAYLOAD)), ("zstd", zstd.compress(PAYLOAD))],
)
def test_a_compressed_answer_is_decoded(monkeypatch: pytest.MonkeyPatch, encoding: str, body: bytes) -> None:
    # The scrapers refuse a non-public address, and the fake answers on loopback.
    monkeypatch.setattr(base, "is_public_ip", lambda _ip: True)

    decoded = anyio.run(_get_json_encoded, encoding, body)

    assert decoded == {"title": "Album"}
