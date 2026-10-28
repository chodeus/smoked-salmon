"""Suggested answers: every prompt stays, but the answer the files already imply is pre-typed."""

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import anyio
import asyncclick as click
import pytest
from test_tagger_metadata_source_url import make_release_data

import salmon.tagger as tagger
import salmon.trackers as trackers
from salmon.common.strings import comparable
from salmon.errors import ScrapeError
from salmon.sources.qobuz import QobuzBase
from salmon.tagger import metadata as metadata_mod
from salmon.tagger import review
from salmon.tagger import tag_urls as src
from salmon.uploader import dupe_checker, staging

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


def test_files_store_url_reads_the_files_store_tag(album_dir, monkeypatch) -> None:
    monkeypatch.setattr(src, "MutagenFile", lambda _path: _FakeAudio({"SOURCE": [QOBUZ_URL]}))

    assert metadata_mod.files_store_url(str(album_dir)) == QOBUZ_URL


def test_files_store_url_ignores_tags_that_are_not_urls(album_dir, monkeypatch) -> None:
    tags = {"source": "Qobuz", "url": "not a url", "comment": f"Bought at {QOBUZ_URL}"}
    monkeypatch.setattr(src, "MutagenFile", lambda _path: _FakeAudio(tags))

    assert metadata_mod.files_store_url(str(album_dir)) is None


TIDAL_TRACK = "https://tidal.com/browse/track/497503885"
TIDAL_ALBUM = "https://tidal.com/browse/album/497503881"
AMAZON_URL = "https://www.amazon.com/dp/B000123456"
MB_RELEASE = "https://musicbrainz.org/release/0a1b2c3d-0000-4000-8000-000000000000"
DISCOGS_RELEASE = "https://www.discogs.com/release/1234567"


def test_release_type_from_folder_reads_the_library_layout() -> None:
    assert review.release_type_from_folder("/data/media/music/Illy/EP/(2022) journaling") == "EP"
    assert review.release_type_from_folder("/data/torrents/salmon/(2022) journaling") is None


def test_the_library_folder_names_the_release_type_whatever_the_files_imply() -> None:
    assert review.suggest_release_type("EP", "Album", [200] * 12) == "EP"


# With no library folder, the files decide.
@pytest.mark.parametrize(
    ("title", "durations", "expected"),
    [
        ("Album", [200], "Single"),
        ("Album", [200, 200, 200], "Single"),
        ("Album", [200] * 4, "EP"),
        ("Album", [200] * 6, "EP"),
        ("Album", [200] * 7, "Album"),
        # Past 30 minutes, an album; a single's track past 10 minutes makes it an EP.
        ("Album", [400] * 5, "Album"),
        ("Album", [700, 200], "EP"),
        # Unknown lengths: the count alone.
        ("Album", [0, 0], "Single"),
        # The title agrees.
        ("Album EP", [200] * 4, "EP"),
        ("Album (Single)", [200], "Single"),
        # The title names another type.
        ("Album EP", [200] * 12, None),
        ("Album (Single)", [200] * 5, None),
        ("Album", [], None),
    ],
)
def test_the_release_type_follows_the_track_count(title, durations, expected) -> None:
    assert review.suggest_release_type(None, title, durations) == expected


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
    assert dupe_checker.suggest_group(results, {**release, "year": 2019}) == ""
    assert dupe_checker.suggest_group([], release) == ""
    assert dupe_checker.suggest_group(results, None) == ""


def _sg_result(group_id: int, name: str = "Album", year: Any = 2020, artist: str = "Artist") -> dict:
    return {"groupId": group_id, "groupName": name, "groupYear": year, "artist": artist}


def _sg_release(**overrides: Any) -> dict[str, Any]:
    return {
        "artists": [("Artist", "main"), ("Guest", "guest")],
        "title": "Album",
        "year": "2020",
        "group_year": "2020",
        **overrides,
    }


def test_names_match_as_the_tracker_escapes_them_and_credits_joint_artists() -> None:
    release = _sg_release(title="Rock & Roll", artists=[("A", "main"), ("B", "main")])

    assert dupe_checker.suggest_group([_sg_result(9, name="Rock &amp; Roll", artist="A &amp; B")], release) == "1"


@pytest.mark.parametrize(
    ("results", "release"),
    [
        ([_sg_result(1, year=2015)], _sg_release()),
        ([_sg_result(1)], _sg_release(year=None, group_year=None)),
        ([_sg_result(1), _sg_result(2)], _sg_release()),
        ([_sg_result(1, artist="Guest")], _sg_release()),
    ],
    ids=["another year", "our year unknown", "two groups match", "a guest's group"],
)
def test_no_single_matching_group_leaves_a_new_group_the_default(results, release) -> None:
    assert dupe_checker.suggest_group(results, release) == ""


def test_a_remaster_defaults_to_the_group_of_its_original_year() -> None:
    results = [_sg_result(1, year=2023), _sg_result(2, year=2018)]

    assert dupe_checker.suggest_group(results, _sg_release(year="2023", group_year="2018")) == "2"


def test_without_a_group_year_the_year_is_compared() -> None:
    release = _sg_release(year="2020")
    del release["group_year"]

    assert dupe_checker.suggest_group([_sg_result(1, year=2015), _sg_result(2)], release) == "2"
    assert dupe_checker.suggest_group([_sg_result(1, year=2018)], release) == ""


def test_a_group_of_another_year_than_our_group_year_is_no_default() -> None:
    # The edition year alone does not match when the group year says otherwise.
    assert dupe_checker.suggest_group([_sg_result(1, year=2023)], _sg_release(year="2023", group_year="2018")) == ""


def test_dupe_prompt_pretypes_the_matching_result(monkeypatch) -> None:
    _capturing_prompt(monkeypatch, "salmon.uploader.dupe_checker.click.prompt")
    site = cast("Any", SimpleNamespace(base_url="https://redacted.sh"))
    results = [{"groupId": 100}, {"groupId": 200}]

    result = anyio.run(dupe_checker._prompt_for_group_id, site, results, True, "2")
    assert result == 200
    result = anyio.run(dupe_checker._prompt_for_group_id, site, results, True, "")
    assert result is None


def _search_results(*entries):
    results: dict = {}
    for source, rls_id, ident in entries:
        results.setdefault(source, {})[rls_id] = (ident, "display")
    return results


def test_metadata_prompt_pretypes_the_suggestion(monkeypatch) -> None:
    defaults = _capturing_prompt(monkeypatch, "salmon.tagger.metadata.click.prompt", answer="m")

    async def manual_metadata(_rls_data):
        return {"tracks": {}}

    monkeypatch.setattr(metadata_mod, "_get_manual_metadata", manual_metadata)

    anyio.run(metadata_mod._select_choice, {}, {"urls": []}, "*" + QOBUZ_URL)

    assert defaults == ["*" + QOBUZ_URL]


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


def test_names_from_the_site_are_compared_with_their_entities_decoded() -> None:
    results = [_sg_result(1, name="Rock &amp; Roll", artist="Artist &amp; Band")]
    release = _sg_release(title="Rock & Roll", artists=[("Artist & Band", "main")])
    assert dupe_checker.suggest_group(results, release) == "1"


def test_an_empty_title_never_pre_types_a_result() -> None:
    results = [{"groupId": 100, "groupName": "", "groupYear": 2022, "artist": "Illy"}]

    assert dupe_checker.suggest_group(results, {"artists": [("Illy", "main")], "title": None, "year": 2022}) == ""


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


def test_the_pretyped_metadata_answer_is_explained(capsys) -> None:
    metadata_mod._explain_default(f"*{QOBUZ_URL} 7", {7: ("Deezer", "1013507111")})

    out = capsys.readouterr().out
    assert f"Pre-typed *{QOBUZ_URL}: the store page in the files' tags" in out
    assert "Pre-typed 7: the Deezer result matching the files' artist and title (and track count and year" in out


def test_the_metadata_prompt_says_how_several_answers_combine(monkeypatch) -> None:
    messages: list[str] = []

    async def fake_prompt(message, **_kwargs):
        messages.append(str(message))
        return "m"

    monkeypatch.setattr("salmon.tagger.metadata.click.prompt", fake_prompt)

    async def manual_metadata(_rls_data):
        return {"tracks": {}}

    monkeypatch.setattr(metadata_mod, "_get_manual_metadata", manual_metadata)

    anyio.run(metadata_mod._select_choice, {1: ("Deezer", "1")}, {"urls": []}, None)

    assert "separated by spaces" in messages[0]
    assert "[a]bort" in messages[0]


def test_an_unstarred_store_url_says_why_it_is_not_the_source(capsys) -> None:
    metadata_mod._explain_default("https://www.qobuz.com/album/x/0887396827479", {})

    out = capsys.readouterr().out
    assert "(not starred: not a WEB release)" in out


def test_a_store_url_that_fails_to_scrape_is_reported_and_left_out(monkeypatch, capsys) -> None:
    class FailingScraper:
        regex = QobuzBase.regex

        async def scrape_release(self, url: str):
            raise ScrapeError("no such album")

    async def answer(*_args, **_kwargs):
        return "https://open.qobuz.com/album/0887396827479"

    monkeypatch.setattr(metadata_mod.click, "prompt", answer)
    monkeypatch.setattr(metadata_mod, "METASOURCES", {"Qobuz": SimpleNamespace(Scraper=FailingScraper)})

    metadata, _source_url = anyio.run(metadata_mod._select_choice, {}, make_release_data())

    out = capsys.readouterr().out
    assert metadata["title"] == "Example Release"
    assert "Scrape error: no such album" in out


def test_salmon_tag_pretypes_the_release_type_its_library_folder_names(monkeypatch, tmp_path) -> None:
    album = tmp_path / "Artist" / "EP" / "Album"
    album.mkdir(parents=True)
    hints = []

    @contextmanager
    def in_place(path, scratch):
        yield path, None

    async def release_data(*_args, **_kwargs):
        return {"title": "Album"}

    async def scraped(*_args):
        return {"title": "Album"}, None

    async def review_then_stop(metadata, rls_data, source_url, validator, review, **_kwargs):
        hints.append(review.keywords["rls_type_hint"])
        raise click.Abort

    monkeypatch.setattr(staging, "staged_source", in_place)
    monkeypatch.setattr(tagger, "standardize_tags", lambda path: None)
    monkeypatch.setattr(tagger, "gather_tags", lambda path: {})
    monkeypatch.setattr(tagger, "gather_audio_info", lambda path: {})
    monkeypatch.setattr(tagger, "construct_rls_data", release_data)
    monkeypatch.setattr(tagger, "get_metadata", scraped)
    monkeypatch.setattr(tagger, "review_metadata_with_ai", review_then_stop)

    with pytest.raises(click.Abort):
        anyio.run(tagger.run_tag, tagger.TagOptions(path=str(album), source="WEB"))

    assert hints == ["EP"]


def test_a_tracked_bandcamp_link_is_the_same_album_as_its_plain_one() -> None:
    plain = metadata_mod._album_identity("https://artist.bandcamp.com/album/some-album")

    assert plain is not None
    assert metadata_mod._album_identity("https://artist.bandcamp.com/album/some-album?from=fanpub_fnb") == plain
    assert metadata_mod._album_identity("https://artist.bandcamp.com/album/some-album#buyFullDiscography") == plain
