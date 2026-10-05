"""Prompts whose default comes from the files when they know the answer (ported from upstream #595).

Every prompt stays and any typed answer still wins; only the answer an empty reply gives changes. Files
that contradict themselves give no default.
"""

from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    _returning,
    _returning_async,
    _write_flac,
)

import salmon.uploader
from salmon.checks.source import detect_source
from salmon.search.base import IdentData
from salmon.tagger import metadata as metadata_mod

QOBUZ_URL = "https://www.qobuz.com/gb-en/album/album-artist/abc123xyz"
DEEZER_URL = "https://www.deezer.com/album/322064097"


def _prompts(monkeypatch, *answers: str) -> list[Any]:
    """Answer click.prompt with `answers` in turn (an empty one takes the default); give the defaults offered."""
    defaults: list[Any] = []
    queue = list(answers)

    async def prompt(*_args: Any, **kwargs: Any) -> Any:
        defaults.append(kwargs.get("default"))
        answer = queue.pop(0) if queue else ""
        return answer or kwargs.get("default")

    monkeypatch.setattr(click, "prompt", prompt)
    return defaults


# The store URL in the tags, and the matching search result: the metadata prompt


def _tagged_album(folder: Path, *tags: dict[str, str]) -> Path:
    """An album of one FLAC per tag set."""
    folder.mkdir(parents=True)
    for number, file_tags in enumerate(tags, 1):
        _write_flac(folder / f"0{number}.flac", title=f"Track {number}", **file_tags)
    return folder


def test_the_store_url_under_a_source_key_is_the_files_store_url(tmp_path) -> None:
    album = _tagged_album(tmp_path / "a", {"SOURCE": QOBUZ_URL}, {"SOURCE": QOBUZ_URL})

    assert metadata_mod.files_store_url(str(album)) == QOBUZ_URL


@pytest.mark.parametrize(
    "tags",
    [
        # Two different store albums.
        ({"SOURCE": QOBUZ_URL}, {"SOURCE": DEEZER_URL}),
        # A second store album under another key.
        ({"SOURCE": QOBUZ_URL, "COMMENT": DEEZER_URL}, {"SOURCE": QOBUZ_URL}),
        # A store's track page, not its album.
        ({"SOURCE": "https://www.deezer.com/track/12345"},),
        # Not a store.
        ({"SOURCE": "https://artist.example/album/abc"},),
        # A store album, but not under a source key.
        ({"COMMENT": QOBUZ_URL},),
    ],
)
def test_files_that_name_no_single_store_album_give_no_store_url(tmp_path, tags) -> None:
    album = _tagged_album(tmp_path / "a", *tags)

    assert metadata_mod.files_store_url(str(album)) is None


def _rls_data(**overrides: Any) -> dict[str, Any]:
    return {
        "artists": [("Artist", "main"), ("Guest", "guest")],
        "title": "Album",
        "year": "2020",
        "group_year": "2020",
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "edition_title": None,
        "label": None,
        "catno": None,
        "upc": None,
        "genres": [],
        "rls_type": None,
        "comment": None,
        "urls": [],
        "tracks": {},
        **overrides,
    }


def _search(*entries: tuple[str, str, IdentData]) -> tuple[dict[int, tuple[str, str]], dict[str, Any]]:
    """Numbered choices and search results, from (source, release ID, ident) entries."""
    choices: dict[int, tuple[str, str]] = {}
    results: dict[str, Any] = {}
    for number, (source, rls_id, ident) in enumerate(entries, 1):
        choices[number] = (source, rls_id)
        results.setdefault(source, {})[rls_id] = (ident, "display")
    return choices, results


def _ident(artist: str = "Artist", album: str = "Album", year: Any = 2020, tracks: int | None = 2) -> IdentData:
    return IdentData(artist, album, year, tracks, "WEB")


def test_the_store_url_is_starred_and_the_matching_result_of_another_source_added() -> None:
    choices, results = _search(("MusicBrainz", "mb", _ident(album="Other")), ("Deezer", "dz", _ident()))

    suggestion = metadata_mod.suggest_choice(choices, results, _rls_data(), 2, QOBUZ_URL)
    assert suggestion == f"*{QOBUZ_URL} 2"


def test_a_store_url_is_not_starred_for_a_release_that_is_not_web() -> None:
    suggestion = metadata_mod.suggest_choice({}, {}, _rls_data(source="CD"), 2, QOBUZ_URL)
    assert suggestion == QOBUZ_URL


def test_the_matching_result_of_the_store_urls_own_source_is_left_out() -> None:
    choices, results = _search(("Deezer", "dz", _ident()))

    suggestion = metadata_mod.suggest_choice(choices, results, _rls_data(), 2, DEEZER_URL)
    assert suggestion == f"*{DEEZER_URL}"


def test_a_store_suffix_on_the_title_and_a_joint_artist_credit_still_match() -> None:
    rls_data = _rls_data(artists=[("Artist", "main"), ("Other", "main")])
    choices, results = _search(("Apple Music", "am", _ident(artist="Artist & Other", album="Album - EP")))

    assert metadata_mod.suggest_choice(choices, results, rls_data, 2, None) == "1"


@pytest.mark.parametrize(
    "ident",
    [
        _ident(tracks=3),  # Another track count.
        _ident(year=2015),  # Another year.
        _ident(artist="Guest"),  # Only a guest artist.
        _ident(album="Album Two"),
    ],
)
def test_a_result_the_files_contradict_is_not_the_default(ident) -> None:
    choices, results = _search(("Deezer", "dz", ident))

    assert metadata_mod.suggest_choice(choices, results, _rls_data(), 2, None) is None


def test_two_matching_results_of_one_source_give_none_of_it() -> None:
    choices, results = _search(("Deezer", "explicit", _ident()), ("Deezer", "clean", _ident()))

    assert metadata_mod.suggest_choice(choices, results, _rls_data(), 2, None) is None


def test_the_metadata_prompt_offers_the_files_store_url_and_the_matching_result(monkeypatch, tmp_path) -> None:
    album = _tagged_album(tmp_path / "a", {"SOURCE": QOBUZ_URL}, {"SOURCE": QOBUZ_URL})
    choices_found = {"Deezer": {"dz": (_ident(), "Artist - Album")}}
    monkeypatch.setattr(metadata_mod, "run_metasearch", _returning_async(choices_found))
    monkeypatch.setattr(metadata_mod, "_get_manual_metadata", _returning({"tracks": {}, "genres": []}))
    # A [m]anual answer: what the prompt offered is all this checks.
    defaults = _prompts(monkeypatch, "m")

    anyio.run(metadata_mod.get_metadata, str(album), {"01.flac": None, "02.flac": None}, _rls_data())
    assert defaults == [f"*{QOBUZ_URL} 1"]


def test_with_no_store_url_and_no_match_the_metadata_prompt_has_no_default(monkeypatch, tmp_path) -> None:
    album = _tagged_album(tmp_path / "a", {"SOURCE": QOBUZ_URL}, {"SOURCE": DEEZER_URL})
    choices_found = {"Deezer": {"dz": (_ident(tracks=9), "Artist - Album")}}
    monkeypatch.setattr(metadata_mod, "run_metasearch", _returning_async(choices_found))
    monkeypatch.setattr(metadata_mod, "_get_manual_metadata", _returning({"tracks": {}, "genres": []}))
    defaults = _prompts(monkeypatch, "m")

    anyio.run(metadata_mod.get_metadata, str(album), {"01.flac": None, "02.flac": None}, _rls_data())
    assert defaults == [None]


def test_one_store_url_gives_both_the_media_default_and_the_metadata_default(monkeypatch, tmp_path) -> None:
    # The source prompt's default is #537's detection; the metadata prompt's comes from the same tag.
    album = _tagged_album(tmp_path / "a", {"SOURCE": QOBUZ_URL}, {"SOURCE": QOBUZ_URL})
    monkeypatch.setattr(metadata_mod, "run_metasearch", _returning_async({}))
    monkeypatch.setattr(metadata_mod, "_get_manual_metadata", _returning({"tracks": {}, "genres": []}))
    defaults = _prompts(monkeypatch, "", "m")

    source = anyio.run(salmon.uploader._prompt_source, detect_source(str(album)))
    anyio.run(metadata_mod.get_metadata, str(album), {"01.flac": None, "02.flac": None}, _rls_data(source=source))
    assert source == "WEB"
    assert defaults == ["WEB", f"*{QOBUZ_URL}"]
