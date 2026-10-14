from typing import Any

import asyncclick as click

from salmon.errors import ScrapeError
from salmon.tagger.sources import (
    apple_music,
    bandcamp,
    beatport,
    deezer,
    discogs,
    musicbrainz,
    qobuz,
    tidal,
)

METASOURCES = {
    "MusicBrainz": musicbrainz,
    "Apple Music": apple_music,
    "Deezer": deezer,
    "Discogs": discogs,
    "Beatport": beatport,
    "Qobuz": qobuz,
    "Tidal": tidal,
    "Bandcamp": bandcamp,  # Must be last due to the catch-all nature of its URLs.
}


def _without_artist_variations(metadata: dict[str, Any]) -> dict[str, Any]:
    """Drop the Discogs ANVs a scraper keeps for the merge with file tags; a scrape used alone has no tags to match."""
    for disc in metadata["tracks"].values():
        for track in disc.values():
            track.pop("artist_variations", None)
    return metadata


async def run_metadata(
    url: str,
    sources: dict[str, Any] | None = None,
    return_source_name: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], str]:
    """Run a scrape for the metadata of a URL.

    Args:
        url: The URL to scrape metadata from.
        sources: Optional dict of sources to use, defaults to all.
        return_source_name: If True, return tuple of (metadata, source_name).

    Returns:
        Metadata dict, or tuple of (metadata, source_name) if return_source_name is True.

    Raises:
        ScrapeError: If URL doesn't match any scraper.
    """
    sources = METASOURCES if not sources else {name: source for name, source in METASOURCES.items() if name in sources}
    for name, source in sources.items():
        if source.Scraper.regex.match(url):
            click.secho(f"Getting metadata from {name}.", fg="cyan")
            metadata = _without_artist_variations(await source.Scraper().scrape_release(url))
            return (metadata, name) if return_source_name else metadata
    raise ScrapeError("URL did not match a scraper.")
