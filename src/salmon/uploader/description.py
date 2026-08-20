"""Tracklist description generation, shared by `salmon descgen` and the web interface."""

import asyncio

from salmon.common import str_to_int_if_int
from salmon.tagger.combine import combine_metadatas
from salmon.tagger.metadata import clean_metadata, remove_various_artists
from salmon.tagger.retagger import create_artist_str
from salmon.tagger.sources import run_metadata
from salmon.uploader.upload import generate_source_links

# One scrape per URL; keep a caller from opening an unbounded number at once.
MAX_CONCURRENT_SCRAPES = 4


async def build_tracklist_description(urls: tuple[str, ...] | list[str]) -> str:
    """Scrape each URL, merge the metadata, and render a BBCode tracklist."""
    limit = asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)

    async def scrape(url: str):
        async with limit:
            return await run_metadata(url, return_source_name=True)

    metadatas = await asyncio.gather(*[scrape(url) for url in urls])
    metadata = clean_metadata(combine_metadatas(*((source, meta) for meta, source in metadatas)))
    remove_various_artists(metadata["tracks"])

    description = "[b][size=4]Tracklist[/b]\n\n"
    multi_disc = len(metadata["tracks"]) > 1
    for dnum, disc in metadata["tracks"].items():
        for tnum, track in disc.items():
            if multi_disc:
                description += (
                    f"[b]{str_to_int_if_int(str(dnum), zpad=True)}-{str_to_int_if_int(str(tnum), zpad=True)}.[/b] "
                )
            else:
                description += f"[b]{str_to_int_if_int(str(tnum), zpad=True)}.[/b] "
            description += f"{create_artist_str(track['artists'])} - {track['title']}\n"
    if metadata["comment"]:
        description += f"\n{metadata['comment']}\n"
    if metadata["urls"]:
        description += "\n[b]More info:[/b] " + generate_source_links(metadata["urls"])
    return description
