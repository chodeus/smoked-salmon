"""Metadata search and release scraping endpoints."""

from typing import Any

from fastapi import APIRouter, HTTPException

from salmon.errors import ScrapeError
from salmon.search import SEARCHSOURCES, run_metasearch
from salmon.tagger.sources import run_metadata

router = APIRouter(tags=["search"])


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
    try:
        meta = await run_metadata(url)
    except ScrapeError as e:
        raise HTTPException(status_code=422, detail=f"Scrape failed: {e}") from e
    if meta is None:
        raise HTTPException(status_code=422, detail="Unsupported URL or scrape returned nothing.")
    return {"url": url, "metadata": meta}
