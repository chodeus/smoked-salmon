"""What this fork's descgen adds to upstream's: a few scrapes at once, however many URLs it is given."""

import asyncio

import anyio

import salmon.commands
from salmon.commands import MAX_CONCURRENT_SCRAPES, build_description

METADATA = {"tracks": {"1": {"1": {"artists": [("Artist", "main")], "title": "First"}}}, "comment": None, "urls": []}


def test_build_description_scrapes_a_few_urls_at_once(monkeypatch) -> None:
    running = 0
    most = 0

    async def run_metadata(url, return_source_name=False):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return METADATA, "Qobuz"

    monkeypatch.setattr(salmon.commands, "run_metadata", run_metadata)
    monkeypatch.setattr(salmon.commands, "combine_metadatas", lambda *_: METADATA)
    monkeypatch.setattr(salmon.commands, "clean_metadata", lambda found: found)

    description = anyio.run(build_description, [f"https://www.qobuz.com/album/x{n}" for n in range(10)])

    assert "First" in description
    assert most == MAX_CONCURRENT_SCRAPES
