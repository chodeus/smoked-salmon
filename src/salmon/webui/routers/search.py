"""Metadata search and release scraping endpoints."""

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException

from salmon.errors import ScrapeError
from salmon.search import SEARCHSOURCES, run_metasearch
from salmon.tagger.sources import run_metadata

router = APIRouter(tags=["search"])

_BLOCKED_IP_ATTRS = ("is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(getattr(ip, attr) for attr in _BLOCKED_IP_ATTRS)


async def _assert_public_url(url: str) -> None:
    """Reject non-http(s) URLs and any host resolving to a private/internal address.

    Bandcamp uses per-artist custom domains, so scrapers match arbitrary hosts;
    without this, /api/metadata?url=http://<internal-host>/album/x is an SSRF
    that makes the server fetch internal services.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=422, detail="Only http(s) URLs are supported.")
    host = parsed.hostname
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        except socket.gaierror as e:
            raise HTTPException(status_code=422, detail=f"Could not resolve host: {host}") from e
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]
    if any(_is_blocked_ip(addr) for addr in addrs):
        raise HTTPException(status_code=422, detail="Refusing to fetch a non-public address.")


@router.get("/search")
async def search(q: str, limit: int = 10, track_count: int | None = None) -> dict:
    """Search all configured metadata providers for a release."""
    results = await run_metasearch([q], limit=limit, track_count=track_count, filter=False)
    sources: dict[str, Any] = {}
    for name in SEARCHSOURCES:
        result = results.get(name)
        if result is None:
            sources[name] = {"active": False, "releases": []}
            continue
        searcher = SEARCHSOURCES[name].Searcher
        releases = []
        for rls_id, (ident, _display) in result.items():
            releases.append(
                {
                    "id": str(rls_id),
                    "artist": ident.artist,
                    "album": ident.album,
                    "year": ident.year,
                    "track_count": ident.track_count,
                    "source": ident.source,
                    "url": searcher.format_url(rls_id),
                }
            )
        sources[name] = {"active": True, "releases": releases}
    return {"query": q, "sources": sources}


@router.get("/metadata")
async def metadata(url: str) -> dict:
    """Scrape full release metadata from a supported source URL."""
    await _assert_public_url(url)
    try:
        meta = await run_metadata(url)
    except ScrapeError as e:
        raise HTTPException(status_code=422, detail=f"Scrape failed: {e}") from e
    if meta is None:
        raise HTTPException(status_code=422, detail="Unsupported URL or scrape returned nothing.")
    return {"url": url, "metadata": meta}
