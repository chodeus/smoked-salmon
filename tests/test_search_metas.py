"""`salmon metas` prints one line per release found. No store is contacted."""

import anyio

import salmon.search
from salmon.search import metas
from salmon.search.base import IdentData

RELEASE_IDS = {
    "Bandcamp": ("artist.bandcamp.com", "album", "the-album"),
    "MusicBrainz": "00000000-0000-0000-0000-000000000001",
    "Apple Music": ("us", "en-US", "1234567890"),
    "Discogs": 123456,
    "Beatport": 654321,
    "Qobuz": "abc123",
    "Tidal": ("US", 987654),
    "Deezer": 192837,
}


def test_metas_prints_every_result_url(monkeypatch, capsys) -> None:
    async def fake_run_metasearch(searchstrs, limit, track_count):
        return {
            source: {
                rls_id: (
                    IdentData("Artist", "The Album", 2024, 10, source),
                    f"[{source} edition]",
                )
            }
            for source, rls_id in RELEASE_IDS.items()
        }

    monkeypatch.setattr(salmon.search, "run_metasearch", fake_run_metasearch)
    assert metas.callback is not None

    anyio.run(metas.callback, ("artist", "album"), None, 3)

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("> ")]
    assert lines == [
        "> [Bandcamp edition] https://artist.bandcamp.com/album/the-album",
        "> [MusicBrainz edition] https://musicbrainz.org/release/00000000-0000-0000-0000-000000000001",
        "> [Apple Music edition] https://music.apple.com/us/album/-/1234567890",
        "> [Discogs edition] https://www.discogs.com/release/123456",
        "> [Beatport edition] https://beatport.com/release/the-album/654321",
        "> [Qobuz edition] https://www.qobuz.com/album/-/abc123",
        "> [Tidal edition] https://listen.tidal.com/album/987654",
        "> [Deezer edition] https://www.deezer.com/album/192837",
    ]
