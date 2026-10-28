"""Content pairing: what upstream's pairing tests leave unchecked."""

import asyncio
from copy import deepcopy

import anyio
from test_tagger_discogs import make_soup
from test_tagger_track_pairing import _numbered_for_another_edition, _track

from salmon import interaction
from salmon.tagger import retagger
from salmon.tagger.combine import combine_tracks
from salmon.tagger.retagger import tag_files
from salmon.tagger.sources.discogs import Scraper as DiscogsScraper


def test_enter_at_the_move_question_leaves_numbered_files_as_they_are(monkeypatch, capsys):
    tags, metadata = _numbered_for_another_edition()
    written = []

    async def enter(text, **kwargs):
        return kwargs.get("default")

    monkeypatch.setattr(interaction, "confirm", enter)
    monkeypatch.setattr(retagger, "print_changes", lambda *args: None)
    monkeypatch.setattr(retagger, "retag_files", lambda path, album, tracks: written.append(tracks))
    monkeypatch.setattr(retagger, "collect_album_data", lambda metadata: {})
    anyio.run(lambda: tag_files("/unused", tags, metadata, auto_rename=True))

    assert written == []
    assert "Skipping retagging procedure" in capsys.readouterr().out


def test_combine_tracks_shows_how_it_paired_the_tracks(capsys):
    base = {"1": {"1": _track("Django", "1"), "2": _track("Work", "2")}}
    meta = {
        "1": {
            "1": _track("Work (Original Mix)", "1", isrc="GBAAA2600001"),
            "2": _track("Django (Original Mix)", "2", isrc="GBAAA2600002"),
        }
    }

    combine_tracks(deepcopy(base), deepcopy(meta), True)

    out = capsys.readouterr().out
    assert "Matched local tracks to the metadata source:" in out
    assert "1. Work (Original Mix) <- Work (was track 2, matched on partial title)" in out
    assert "2. Django (Original Mix) <- Django (was track 1, matched on partial title)" in out


def test_discogs_tracks_carry_their_duration_in_seconds():
    soup = make_soup([{"type_": "track", "position": "1", "title": "Work", "duration": "4:05"}])

    tracks = asyncio.run(DiscogsScraper().parse_tracks(soup))

    assert tracks["1"]["1"]["duration"] == 245
