"""Suggested answers: every prompt stays, but the answer the files already imply is pre-typed."""

from types import SimpleNamespace
from typing import Any, cast

import anyio
import asyncclick as click
import pytest

import salmon.trackers as trackers
import salmon.uploader as uploader
from salmon import cfg
from salmon.checks import source as src
from salmon.common.strings import comparable
from salmon.search.base import IdentData
from salmon.tagger import metadata as metadata_mod
from salmon.tagger import review
from salmon.uploader import dupe_checker
from salmon.uploader import spectrals as sp

QOBUZ_URL = "https://www.qobuz.com/au-en/album/journaling-illy/ul39e7xjbuqrb"
DEEZER_URL = "https://www.deezer.com/album/322064097"


class _FakeAudio:
    def __init__(self, tags):
        self.tags = tags
        self.info = SimpleNamespace(bits_per_sample=24, sample_rate=44100)


def _capturing_prompt(monkeypatch, target: str, answer=None):
    """Replace click.prompt at `target`; returns the list of defaults it was offered."""
    defaults: list = []

    async def fake_prompt(*_args, **kwargs):
        defaults.append(kwargs.get("default"))
        return kwargs.get("default") if answer is None else answer

    monkeypatch.setattr(target, fake_prompt)
    return defaults


def test_comparable_ignores_case_accents_and_punctuation() -> None:
    assert comparable("Illy – journaling!") == "illyjournaling"
    assert comparable("Café Tacvba") == "cafetacvba"
    assert comparable(None) == ""


def test_store_url_reads_the_files_store_tag(album_dir, monkeypatch) -> None:
    monkeypatch.setattr(src, "MutagenFile", lambda _path: _FakeAudio({"SOURCE": [QOBUZ_URL]}))

    assert src.store_url(str(album_dir)) == QOBUZ_URL


def test_store_url_ignores_tags_that_are_not_urls(album_dir, monkeypatch) -> None:
    monkeypatch.setattr(src, "MutagenFile", lambda _path: _FakeAudio({"source": "Qobuz", "url": "not a url"}))

    assert src.store_url(str(album_dir)) is None


def test_release_type_from_folder_reads_the_library_layout() -> None:
    assert review.release_type_from_folder("/data/media/music/Illy/EP/(2022) journaling") == "EP"
    assert review.release_type_from_folder("/data/torrents/salmon/(2022) journaling") is None


@pytest.mark.parametrize(
    ("folder_hint", "track_count", "expected"),
    [("EP", 12, "EP"), (None, 1, "Single"), (None, 2, "Single"), (None, 6, "EP"), (None, 7, "Album")],
)
def test_suggest_release_type(folder_hint, track_count, expected) -> None:
    assert review.suggest_release_type(folder_hint, track_count) == expected


def test_release_type_prompt_pretypes_the_hint(monkeypatch) -> None:
    defaults = _capturing_prompt(monkeypatch, "salmon.tagger.review.click.prompt")
    metadata = {"rls_type": None}

    anyio.run(review._edit_release_type, metadata, "EP")

    assert metadata["rls_type"] == "EP"
    assert defaults == ["EP"]


def test_suggest_group_picks_the_matching_result_or_a_new_group() -> None:
    results = [
        {"groupId": 1, "groupName": "journaling", "groupYear": 2021, "artist": "Illy"},
        {"groupId": 2, "groupName": "Journaling", "groupYear": 2022, "artist": "ILLY"},
    ]
    release = {"artists": [("Illy", "main")], "title": "journaling", "year": 2022}

    assert dupe_checker.suggest_group(results, release) == "2"
    assert dupe_checker.suggest_group(results, {**release, "year": 2019}) == "N"
    assert dupe_checker.suggest_group([], release) == "N"
    assert dupe_checker.suggest_group(results, None) == "N"


def test_dupe_prompt_pretypes_the_matching_result(monkeypatch) -> None:
    _capturing_prompt(monkeypatch, "salmon.uploader.dupe_checker.click.prompt")
    site = cast("Any", SimpleNamespace(base_url="https://redacted.sh"))
    results = [{"groupId": 100}, {"groupId": 200}]

    result = anyio.run(dupe_checker._prompt_for_group_id, site, results, True, "2")
    assert result == 200
    result = anyio.run(dupe_checker._prompt_for_group_id, site, results, True, "N")
    assert result is None


def _search_results(*entries):
    results: dict = {}
    for source, rls_id, ident in entries:
        results.setdefault(source, {})[rls_id] = (ident, "display")
    return results


def test_suggest_choice_stars_the_store_url_and_adds_the_matching_result() -> None:
    ident = IdentData("Illy", "journaling", 2022, 6, "WEB")
    choices = {1: ("MusicBrainz", "mb1"), 2: ("Deezer", "dz1")}
    results = _search_results(("MusicBrainz", "mb1", ident), ("Deezer", "dz1", ident))
    rls_data = {"artists": [("Illy", "main")], "title": "journaling"}

    assert metadata_mod.suggest_choice(choices, results, rls_data, 6, QOBUZ_URL) == f"*{QOBUZ_URL} 1"
    assert metadata_mod.suggest_choice(choices, results, rls_data, 6, None) == "1"
    assert metadata_mod.suggest_choice(choices, results, rls_data, 9, QOBUZ_URL) == f"*{QOBUZ_URL}"
    assert metadata_mod.suggest_choice({}, {}, rls_data, 6, None) is None


def test_suggest_choice_does_not_add_the_result_the_starred_url_already_is() -> None:
    ident = IdentData("Illy", "journaling", 2022, 6, "WEB")
    choices = {1: ("Deezer", "dz1")}
    results = _search_results(("Deezer", "dz1", ident))
    rls_data = {"artists": [("Illy", "main")], "title": "journaling"}

    assert metadata_mod.suggest_choice(choices, results, rls_data, 6, DEEZER_URL) == f"*{DEEZER_URL}"


def test_metadata_prompt_pretypes_the_suggestion(monkeypatch) -> None:
    defaults = _capturing_prompt(monkeypatch, "salmon.tagger.metadata.click.prompt", answer="m")
    monkeypatch.setattr(metadata_mod, "_get_manual_metadata", lambda rls_data: {"tracks": {}})

    anyio.run(metadata_mod._select_choice, {}, {"urls": []}, "*" + QOBUZ_URL)

    assert defaults == ["*" + QOBUZ_URL]


def test_lossy_prompt_pretypes_the_analysis_verdict(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(sp, "flush_stdin", lambda: None)
    defaults = _capturing_prompt(monkeypatch, "salmon.uploader.spectrals.click.prompt")

    result = anyio.run(sp.prompt_lossy_master, False, "y")
    assert result is True
    result = anyio.run(sp.prompt_lossy_master, False, "n")
    assert result is False
    assert defaults == ["y", "n"]


def test_source_prompt_pretypes_a_confirmed_detection(monkeypatch) -> None:
    defaults = _capturing_prompt(monkeypatch, "salmon.uploader.click.prompt")
    detected = {"source": "WEB", "confidence": "confirmed", "reasons": ['Files declare media "digital media".']}

    result = anyio.run(uploader._prompt_source, detected)
    assert result == "WEB"
    assert defaults == ["web"]


def test_source_prompt_offers_nothing_for_an_unconfirmed_detection(monkeypatch) -> None:
    defaults = _capturing_prompt(monkeypatch, "salmon.uploader.click.prompt", answer="cd")

    result = anyio.run(uploader._prompt_source, {"source": None, "confidence": "unknown", "reasons": []})
    assert result == "CD"
    assert defaults == [""]


def test_next_tracker_takes_the_preselected_site_without_asking(monkeypatch) -> None:
    async def never(_choices):
        raise AssertionError("no tracker question when the trackers were picked up front")

    monkeypatch.setattr(trackers, "choose_tracker", never)

    result = anyio.run(uploader.next_tracker, True, ["OPS"])
    assert result == "OPS"


def test_next_tracker_asks_when_nothing_was_preselected(monkeypatch) -> None:
    asked: list[list[str]] = []

    async def choose(choices):
        asked.append(list(choices))
        return "OPS"

    monkeypatch.setattr(trackers, "choose_tracker", choose)

    result = anyio.run(uploader.next_tracker, False, ["OPS", "DIC"])
    assert result == "OPS"
    assert asked == [["OPS", "DIC"]]


def test_validate_trackers_accepts_the_flag_repeated(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED", "OPS"])

    result = anyio.run(trackers.validate_trackers, None, "tracker", ("red", "OPS", "red"))
    assert result == ("RED", "OPS")


def test_validate_trackers_empty_flag_uses_the_first_time_flow(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED"])

    result = anyio.run(trackers.validate_trackers, None, "tracker", ())
    assert result == ("RED",)


def test_comparable_keeps_letters_of_every_script() -> None:
    assert comparable("宇多田ヒカル") != comparable("坂本龍一")
    assert comparable("Ólafur Arnalds") == comparable("olafur arnalds")
    assert comparable(None) == "" and comparable(0) == "0"


def test_an_empty_title_never_pre_types_a_result() -> None:
    results = [{"groupId": 100, "groupName": "", "groupYear": 2022, "artist": "Illy"}]

    assert dupe_checker.suggest_group(results, {"artists": [("Illy", "main")], "title": None, "year": 2022}) == "N"


def test_suggest_group_skips_a_result_without_a_group_id() -> None:
    release = {"artists": [("Illy", "main")], "title": "journaling", "year": 2022}
    results = [
        {"groupName": "journaling", "groupYear": 2022, "artist": "Illy"},
        {"groupId": 200, "groupName": "journaling", "groupYear": 2022, "artist": "Illy"},
    ]

    assert dupe_checker.suggest_group(results, release) == "2"


def test_validate_trackers_aborts_when_the_choice_is_none(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED"])

    async def nothing_chosen():
        return None

    monkeypatch.setattr(trackers, "choose_tracker_first_time", nothing_chosen)

    with pytest.raises(click.Abort):
        anyio.run(trackers.validate_trackers, None, "tracker", ())
