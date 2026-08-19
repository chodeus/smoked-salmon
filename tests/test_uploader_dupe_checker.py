"""Tests for salmon.uploader.dupe_checker.

Covers search string generation, recent-upload log dupe checking, site search,
and every user-choice branch of the group-selection prompts.
"""

import asyncclick as click
import pytest

from salmon import cfg
from salmon.errors import AbortAndDeleteFolder, RequestError
from salmon.uploader.dupe_checker import (
    _confirm_group_id,
    _prompt_for_group_id,
    _prompt_for_recent_upload_results,
    _sanitize_album_for_dupe_check,
    check_existing_group,
    dupe_check_recent_torrents,
    filter_unnecessary_searchstrs,
    generate_dupe_check_searchstrs,
    get_search_results,
    print_recent_upload_results,
    print_search_results,
    print_torrents,
)

# ---------------------------------------------------------------------------
# Local helpers / fixtures
# ---------------------------------------------------------------------------

USE_DEFAULT = object()


class PromptQueue:
    """Async stand-in for asyncclick.prompt fed from a fixed response list."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, *_args, **kwargs):
        self.calls += 1
        assert self.responses, "click.prompt called more often than responses were queued"
        value = self.responses.pop(0)
        if value is USE_DEFAULT:
            return kwargs.get("default", "")
        return value


@pytest.fixture
def install_prompt(monkeypatch):
    """Patch click.prompt in the module under test with a queued fake."""

    def _install(*responses):
        queue = PromptQueue(responses)
        monkeypatch.setattr("salmon.uploader.dupe_checker.click.prompt", queue)
        return queue

    return _install


def install_log(fake_tracker, uploads):
    """Attach an async get_uploads_from_log returning fixed tuples."""

    async def get_uploads_from_log():
        return uploads

    fake_tracker.get_uploads_from_log = get_uploads_from_log


def install_redirect(fake_tracker, mapping):
    """Attach get_redirect_torrentgroupid resolving via mapping (str keys).

    Exception values are raised; returns the list of received torrent ids.
    """
    calls = []

    async def get_redirect_torrentgroupid(torrent_id):
        calls.append(torrent_id)
        result = mapping[str(torrent_id)]
        if isinstance(result, Exception):
            raise result
        return result

    fake_tracker.get_redirect_torrentgroupid = get_redirect_torrentgroupid
    return calls


def make_result(group_id=100, artist="Artist", name="Album", year=2024):
    """A browse-style search result with everything the printers need."""
    return {
        "groupId": group_id,
        "artist": artist,
        "groupName": name,
        "groupYear": year,
        "releaseType": "Album",
        "tags": ["electronic"],
        "torrents": [{"media": "WEB", "format": "FLAC", "encoding": "Lossless"}],
    }


def make_torrentgroup_response(group_id=555, artist="Artist", name="Album", year=2024):
    """A torrentgroup-ajax-style response as consumed by print_torrents."""
    return {
        "group": {
            "id": group_id,
            "name": name,
            "year": year,
            "musicInfo": {"artists": [{"name": artist}]},
        },
        "torrents": [{"media": "CD", "format": "FLAC", "encoding": "Lossless"}],
    }


ARTIST = [["Artist", "main"]]


# ---------------------------------------------------------------------------
# generate_dupe_check_searchstrs
# ---------------------------------------------------------------------------


def test_searchstrs_single_artist_simple_album():
    assert generate_dupe_check_searchstrs(ARTIST, "Album") == ["artist album"]


def test_searchstrs_two_main_artists_yield_one_string_each():
    artists = [["Foo", "main"], ["Bar", "main"]]
    assert generate_dupe_check_searchstrs(artists, "Baz") == ["foo baz", "bar baz"]


def test_searchstrs_various_artists_uses_album_only():
    artists = [["Various Artists", "main"]]
    assert generate_dupe_check_searchstrs(artists, "Compilation Hits") == ["compilation hits"]


def test_searchstrs_guest_only_artists_use_album_only():
    artists = [["Artist", "guest"]]
    assert generate_dupe_check_searchstrs(artists, "Album") == ["album"]


def test_searchstrs_more_than_three_main_artists_use_album_only():
    artists = [[a, "main"] for a in "ABCD"]
    assert generate_dupe_check_searchstrs(artists, "Big Split") == ["big split"]


def test_searchstrs_accents_are_normalized_to_ascii():
    artists = [["Björk", "main"]]
    assert generate_dupe_check_searchstrs(artists, "Médulla") == ["bjork medulla"]


def test_searchstrs_special_characters_replaced_with_spaces_dots_kept():
    # re_strip with filter_nonscrape=False: / - \ , become spaces, dots survive
    artists = [["AC/DC", "main"]]
    assert generate_dupe_check_searchstrs(artists, "Rock, Or-Bust.") == ["ac dc rock or bust."]


def test_searchstrs_feat_credit_is_stripped_from_album():
    assert generate_dupe_check_searchstrs(ARTIST, "Song (feat. Someone)") == ["artist song"]


def test_searchstrs_bracketed_edition_is_stripped_leaving_trailing_space():
    # The edition regex removes "(Deluxe Edition)" but the space before it stays.
    assert generate_dupe_check_searchstrs(ARTIST, "Album (Deluxe Edition)") == ["artist album "]


def test_searchstrs_bracketed_remixes_become_remix_marker():
    # "Remix(es)" must not trigger the edition regex's "Mix" keyword; the
    # bracket is replaced with a bare "remix(es)" token instead of removed.
    assert generate_dupe_check_searchstrs(ARTIST, "Album (The Best Remixes)") == ["artist album remixes"]
    assert generate_dupe_check_searchstrs(ARTIST, "Album (Bonus Remix)") == ["artist album remix"]
    assert _sanitize_album_for_dupe_check("Album (The Best Remixes)") == "Album remixes"


def test_searchstrs_genuine_edition_suffixes_still_stripped():
    assert generate_dupe_check_searchstrs(ARTIST, "Album (2024 Remaster)") == ["artist album "]
    assert generate_dupe_check_searchstrs(ARTIST, "Album (Club Mix)") == ["artist album "]


def test_searchstrs_vol_album_gets_extra_volume_spelling():
    assert generate_dupe_check_searchstrs(ARTIST, "Greatest Hits Vol. 2") == [
        "artist greatest hits vol. 2",
        "artist greatest hits volume 2",
    ]


def test_searchstrs_volume_spelled_out_gets_no_extra_string():
    assert generate_dupe_check_searchstrs(ARTIST, "Greatest Hits Volume 2") == ["artist greatest hits volume 2"]


def test_searchstrs_untitled_album_with_catno_adds_catno_search():
    assert generate_dupe_check_searchstrs(ARTIST, "Untitled", "CAT001") == [
        "artist cat001",
        "artist untitled",
    ]


def test_searchstrs_untitled_album_without_catno_collapses_to_artist_only():
    # The empty-catno searchstr "artist " is a word-subset of "artist untitled",
    # so the filter drops the album searchstr entirely.
    assert generate_dupe_check_searchstrs(ARTIST, "Untitled") == ["artist "]


def test_searchstrs_catno_as_album_title_adds_untitled_search():
    assert generate_dupe_check_searchstrs(ARTIST, "CAT001", "CAT001") == [
        "artist cat001",
        "artist untitled",
    ]


def test_searchstrs_empty_catno_adds_nothing():
    assert generate_dupe_check_searchstrs(ARTIST, "Album", "") == ["artist album"]


def test_searchstrs_slash_single_keeps_only_first_side():
    # The full-album string is a word-superset of the first-side string and is
    # filtered out; the split keeps the trailing space before the slash.
    assert generate_dupe_check_searchstrs(ARTIST, "Song A / Song B") == ["artist song a "]


def test_searchstrs_none_album_yields_artist_only():
    assert generate_dupe_check_searchstrs(ARTIST, None) == ["artist "]


def test_searchstrs_empty_album_yields_artist_only():
    assert generate_dupe_check_searchstrs(ARTIST, "") == ["artist "]


def test_filter_unnecessary_removes_exact_duplicates():
    assert filter_unnecessary_searchstrs(["a b", "a b"]) == ["a b"]


def test_filter_unnecessary_drops_word_supersets_and_sorts_by_length():
    # Output is sorted by length ("other thing" is shorter than "artist album"),
    # and the word-superset "artist album deluxe" is dropped.
    assert filter_unnecessary_searchstrs(["artist album deluxe", "artist album", "other thing"]) == [
        "other thing",
        "artist album",
    ]


def test_filter_unnecessary_keeps_partially_overlapping_strings():
    assert filter_unnecessary_searchstrs(["foo baz", "bar baz"]) == ["foo baz", "bar baz"]


def test_sanitize_album_none_and_empty_return_empty_string():
    assert _sanitize_album_for_dupe_check(None) == ""
    assert _sanitize_album_for_dupe_check("") == ""


# ---------------------------------------------------------------------------
# dupe_check_recent_torrents
# ---------------------------------------------------------------------------


async def test_recent_torrents_exact_title_match_is_a_hit(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.5)
    install_log(fake_tracker, [(111, "Artist", "Album")])
    hits = await dupe_check_recent_torrents(fake_tracker, ["artist album"])
    assert hits == [(111, "Artist", "Album")]


async def test_recent_torrents_near_match_is_a_hit(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.5)
    install_log(fake_tracker, [(111, "Artist", "Albun")])
    hits = await dupe_check_recent_torrents(fake_tracker, ["artist album"])
    assert hits == [(111, "Artist", "Albun")]


async def test_recent_torrents_clear_non_match_is_ignored(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.5)
    install_log(fake_tracker, [(111, "Somebody Else", "Different Record")])
    hits = await dupe_check_recent_torrents(fake_tracker, ["artist album"])
    assert hits == []


async def test_recent_torrents_empty_log_returns_empty_list(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.5)
    install_log(fake_tracker, [])
    assert await dupe_check_recent_torrents(fake_tracker, ["artist album"]) == []


async def test_recent_torrents_same_release_counted_once(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.5)
    install_log(fake_tracker, [(1, "Artist", "Album"), (2, "Artist", "Album")])
    hits = await dupe_check_recent_torrents(fake_tracker, ["artist album"])
    assert hits == [(1, "Artist", "Album")]


async def test_recent_torrents_tolerance_config_is_honored(fake_tracker, monkeypatch):
    # "artist albun" vs "artist album" has ratio ~0.917: below a 0.95 bar.
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.95)
    install_log(fake_tracker, [(111, "Artist", "Albun")])
    assert await dupe_check_recent_torrents(fake_tracker, ["artist album"]) == []


async def test_recent_torrents_all_searchstrs_are_compared(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.5)
    install_log(fake_tracker, [(111, "Artist", "Album")])
    hits = await dupe_check_recent_torrents(fake_tracker, ["zzz qqq", "artist album"])
    assert hits == [(111, "Artist", "Album")]


async def test_recent_torrents_any_searchstr_match_counts_once(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.5)
    install_log(fake_tracker, [(1, "Foo", "Baz"), (2, "Bar", "Baz"), (3, "Foo", "Baz"), (4, "Zzz", "Qqq")])
    hits = await dupe_check_recent_torrents(fake_tracker, ["foo baz", "bar baz"])
    assert hits == [(1, "Foo", "Baz"), (2, "Bar", "Baz")]


# ---------------------------------------------------------------------------
# get_search_results
# ---------------------------------------------------------------------------


async def test_get_search_results_returns_hits(fake_tracker):
    result = make_result(100)
    fake_tracker.api_responses["browse"] = {"results": [result]}
    assert await get_search_results(fake_tracker, ["artist album"]) == [result]
    assert fake_tracker.api_calls == [("browse", {"searchstr": "artist album"})]


async def test_get_search_results_zero_hits(fake_tracker):
    fake_tracker.api_responses["browse"] = {"results": []}
    assert await get_search_results(fake_tracker, ["artist album"]) == []


async def test_get_search_results_one_api_call_per_searchstr(fake_tracker):
    fake_tracker.api_responses["browse"] = {"results": []}
    await get_search_results(fake_tracker, ["one", "two", "three"])
    assert [params["searchstr"] for _, params in fake_tracker.api_calls] == ["one", "two", "three"]


async def test_get_search_results_deduplicates_equal_releases_across_searchstrs(fake_tracker):
    r1, r2, r3 = make_result(1), make_result(2), make_result(3)
    per_searchstr = {"one": {"results": [r1, dict(r2)]}, "two": {"results": [dict(r2), r3]}}

    async def api_call(action, params):
        assert action == "browse"
        return per_searchstr[params["searchstr"]]

    fake_tracker.api_call = api_call
    results = await get_search_results(fake_tracker, ["one", "two"])
    # r2 appears in both responses (as equal but distinct dicts) yet only once here.
    assert results == [r1, r2, r3]


async def test_get_search_results_propagates_api_error(fake_tracker):
    fake_tracker.api_responses["browse"] = RequestError("boom")
    with pytest.raises(RequestError):
        await get_search_results(fake_tracker, ["artist album"])


# ---------------------------------------------------------------------------
# _prompt_for_group_id
# ---------------------------------------------------------------------------


async def test_prompt_group_id_pick_listed_result_by_number(fake_tracker, install_prompt):
    install_prompt("2")
    results = [make_result(100), make_result(200)]
    assert await _prompt_for_group_id(fake_tracker, results, True) == 200


async def test_prompt_group_id_zero_is_clamped_to_first_result(fake_tracker, install_prompt):
    install_prompt("0")
    results = [make_result(100), make_result(200)]
    assert await _prompt_for_group_id(fake_tracker, results, True) == 100


async def test_prompt_group_id_number_beyond_results_is_direct_group_id(fake_tracker, install_prompt):
    install_prompt("7777")
    assert await _prompt_for_group_id(fake_tracker, [make_result(100)], True) == 7777


async def test_prompt_group_id_zero_with_no_results_reprompts(fake_tracker, install_prompt):
    queue = install_prompt("0", "n")
    assert await _prompt_for_group_id(fake_tracker, [], True) is None
    assert queue.calls == 2


async def test_prompt_group_id_paste_group_url(fake_tracker, install_prompt):
    install_prompt(f"{fake_tracker.base_url}/torrents.php?id=555")
    assert await _prompt_for_group_id(fake_tracker, [], True) == 555


async def test_prompt_group_id_paste_torrent_url_resolves_group(fake_tracker, install_prompt):
    install_prompt(f"{fake_tracker.base_url}/torrents.php?torrentid=999")
    calls = install_redirect(fake_tracker, {"999": 321})
    assert await _prompt_for_group_id(fake_tracker, [], True) == 321
    assert calls == ["999"]


async def test_prompt_group_id_torrent_url_without_group_reprompts(fake_tracker, install_prompt):
    queue = install_prompt(f"{fake_tracker.base_url}/torrents.php?torrentid=999", "n")
    install_redirect(fake_tracker, {"999": None})
    assert await _prompt_for_group_id(fake_tracker, [], True) is None
    assert queue.calls == 2


async def test_prompt_group_id_url_without_any_id_reprompts(fake_tracker, install_prompt):
    queue = install_prompt(f"{fake_tracker.base_url}/torrents.php?page=2", "n")
    assert await _prompt_for_group_id(fake_tracker, [], True) is None
    assert queue.calls == 2


async def test_prompt_group_id_foreign_url_is_not_treated_as_url(fake_tracker, install_prompt):
    # A URL for a different site matches no branch and simply re-prompts.
    queue = install_prompt("https://other-site.example/torrents.php?id=5", "n")
    assert await _prompt_for_group_id(fake_tracker, [], True) is None
    assert queue.calls == 2


async def test_prompt_group_id_new_group_via_n(fake_tracker, install_prompt):
    install_prompt("N")
    assert await _prompt_for_group_id(fake_tracker, [make_result(100)], True) is None


async def test_prompt_group_id_new_group_via_empty_default(fake_tracker, install_prompt):
    install_prompt(USE_DEFAULT)  # user hits enter, prompt returns default ""
    assert await _prompt_for_group_id(fake_tracker, [], True) is None


async def test_prompt_group_id_abort(fake_tracker, install_prompt):
    install_prompt("a")
    with pytest.raises(click.Abort):
        await _prompt_for_group_id(fake_tracker, [], True)


async def test_prompt_group_id_delete_folder(fake_tracker, install_prompt):
    install_prompt("d")
    with pytest.raises(AbortAndDeleteFolder):
        await _prompt_for_group_id(fake_tracker, [], True)


async def test_prompt_group_id_delete_ignored_when_deletion_not_offered(fake_tracker, install_prompt):
    queue = install_prompt("d", "n")
    assert await _prompt_for_group_id(fake_tracker, [], False) is None
    assert queue.calls == 2


async def test_prompt_group_id_invalid_input_then_valid(fake_tracker, install_prompt):
    queue = install_prompt("xyz", "1")
    assert await _prompt_for_group_id(fake_tracker, [make_result(100)], True) == 100
    assert queue.calls == 2


# ---------------------------------------------------------------------------
# _prompt_for_recent_upload_results
# ---------------------------------------------------------------------------

RECENT = [(111, "Artist", "Album"), (222, "Other", "Record")]


async def test_prompt_recent_pick_listed_upload_resolves_group(fake_tracker, install_prompt):
    install_prompt("1")
    calls = install_redirect(fake_tracker, {"111": 4242})
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True) == 4242
    assert calls == [111]


async def test_prompt_recent_zero_is_treated_as_first_upload(fake_tracker, install_prompt):
    install_prompt("0")
    install_redirect(fake_tracker, {"111": 4242})
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True) == 4242


async def test_prompt_recent_number_beyond_list_is_direct_group_id(fake_tracker, install_prompt):
    install_prompt("9")
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True) == 9


async def test_prompt_recent_numeric_without_uploads_is_direct_group_id(fake_tracker, install_prompt):
    install_prompt("123")
    assert await _prompt_for_recent_upload_results(fake_tracker, [], "artist album", True) == 123


async def test_prompt_recent_zero_without_uploads_reprompts(fake_tracker, install_prompt):
    queue = install_prompt("0", "n")
    assert await _prompt_for_recent_upload_results(fake_tracker, [], "artist album", True) is None
    assert queue.calls == 2


async def test_prompt_recent_redirect_returning_none_reprompts(fake_tracker, install_prompt):
    queue = install_prompt("1", "n")
    install_redirect(fake_tracker, {"111": None})
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True) is None
    assert queue.calls == 2


async def test_prompt_recent_redirect_error_reprompts(fake_tracker, install_prompt):
    queue = install_prompt("1", "n")
    install_redirect(fake_tracker, {"111": RuntimeError("redirect failed")})
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True) is None
    assert queue.calls == 2


async def test_prompt_recent_paste_group_url(fake_tracker, install_prompt):
    install_prompt(f"{fake_tracker.base_url}/torrents.php?id=555")
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True) == 555


async def test_prompt_recent_paste_torrent_url_resolves_group(fake_tracker, install_prompt):
    install_prompt(f"{fake_tracker.base_url}/torrents.php?torrentid=999")
    install_redirect(fake_tracker, {"999": 321})
    assert await _prompt_for_recent_upload_results(fake_tracker, [], "artist album", True) == 321


async def test_prompt_recent_url_without_any_id_reprompts(fake_tracker, install_prompt):
    queue = install_prompt(f"{fake_tracker.base_url}/torrents.php?page=2", "n")
    assert await _prompt_for_recent_upload_results(fake_tracker, [], "artist album", True) is None
    assert queue.calls == 2


async def test_prompt_recent_new_group_via_n(fake_tracker, install_prompt):
    install_prompt("new")
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True) is None


async def test_prompt_recent_new_group_via_empty_input(fake_tracker, install_prompt):
    install_prompt(USE_DEFAULT)
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True) is None


async def test_prompt_recent_abort(fake_tracker, install_prompt):
    install_prompt("a")
    with pytest.raises(click.Abort):
        await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True)


async def test_prompt_recent_delete_folder(fake_tracker, install_prompt):
    install_prompt("d")
    with pytest.raises(AbortAndDeleteFolder):
        await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True)


async def test_prompt_recent_delete_ignored_when_deletion_not_offered(fake_tracker, install_prompt):
    queue = install_prompt("d", "n")
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", False) is None
    assert queue.calls == 2


async def test_prompt_recent_invalid_input_then_valid(fake_tracker, install_prompt):
    queue = install_prompt("xyz", "n")
    assert await _prompt_for_recent_upload_results(fake_tracker, RECENT, "artist album", True) is None
    assert queue.calls == 2


async def test_prompt_recent_lists_at_most_five_uploads(fake_tracker, install_prompt, capsys):
    install_prompt("n")
    uploads = [(i, f"Artist{i}", f"Album{i}") for i in range(1, 8)]
    await _prompt_for_recent_upload_results(fake_tracker, uploads, "artist album", True)
    out = capsys.readouterr().out
    assert "Artist5 - Album5" in out
    assert "Artist6" not in out


# ---------------------------------------------------------------------------
# _confirm_group_id / print_torrents
# ---------------------------------------------------------------------------


async def test_confirm_group_id_yes_uses_search_result_without_api_call(fake_tracker, install_prompt):
    install_prompt("Y")
    assert await _confirm_group_id(fake_tracker, 100, [make_result(100)]) is True
    assert fake_tracker.api_calls == []  # rset came from the search results


async def test_confirm_group_id_default_is_yes(fake_tracker, install_prompt):
    install_prompt(USE_DEFAULT)  # prompt default is "Y"
    assert await _confirm_group_id(fake_tracker, 100, [make_result(100)]) is True


async def test_confirm_group_id_new_group_returns_false(fake_tracker, install_prompt):
    install_prompt("n")
    assert await _confirm_group_id(fake_tracker, 100, [make_result(100)]) is False


async def test_confirm_group_id_abort(fake_tracker, install_prompt):
    install_prompt("a")
    with pytest.raises(click.Abort):
        await _confirm_group_id(fake_tracker, 100, [make_result(100)])


async def test_confirm_group_id_delete_folder(fake_tracker, install_prompt):
    install_prompt("d")
    with pytest.raises(AbortAndDeleteFolder):
        await _confirm_group_id(fake_tracker, 100, [make_result(100)])


async def test_confirm_group_id_invalid_answer_reprompts(fake_tracker, install_prompt):
    queue = install_prompt("zebra", "y")
    assert await _confirm_group_id(fake_tracker, 100, [make_result(100)]) is True
    assert queue.calls == 2


async def test_confirm_group_id_fetches_group_when_not_in_results(fake_tracker, install_prompt):
    install_prompt("Y")
    fake_tracker.api_responses["torrentgroup"] = make_torrentgroup_response(555)
    assert await _confirm_group_id(fake_tracker, 555, [make_result(100)]) is True
    assert fake_tracker.api_calls == [("torrentgroup", {"id": 555})]


async def test_confirm_group_id_unknown_group_aborts(fake_tracker, install_prompt):
    install_prompt("Y")
    fake_tracker.api_responses["torrentgroup"] = RequestError("bad id")
    with pytest.raises(click.Abort):
        await _confirm_group_id(fake_tracker, 12345, [])


async def test_print_torrents_original_release_prefix(fake_tracker, capsys):
    await print_torrents(fake_tracker, 100, make_result(100))
    out = capsys.readouterr().out
    assert "Selected ID: 100" in out
    assert "> OR / WEB / FLAC / Lossless" in out


async def test_print_torrents_remaster_prefix(fake_tracker, capsys):
    rset = make_result(100)
    rset["torrents"] = [
        {
            "remastered": True,
            "remasterYear": 2020,
            "remasterTitle": "Deluxe",
            "remasterRecordLabel": "Label",
            "remasterCatalogueNumber": "CAT-1",
            "media": "CD",
            "format": "FLAC",
            "encoding": "Lossless",
        }
    ]
    await print_torrents(fake_tracker, 100, rset)
    assert "> 2020 / Deluxe / Label / CAT-1 / CD / FLAC / Lossless" in capsys.readouterr().out


async def test_print_torrents_missing_group_raises_abort(fake_tracker, capsys):
    fake_tracker.api_responses["torrentgroup"] = RequestError("404")
    with pytest.raises(click.Abort):
        await print_torrents(fake_tracker, 999)
    assert "999 does not exist." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# print helpers
# ---------------------------------------------------------------------------


def test_print_search_results_no_results_message(fake_tracker, capsys):
    print_search_results(fake_tracker, [], "artist album")
    assert "No groups found on RED" in capsys.readouterr().out


def test_print_search_results_lists_hits_with_urls(fake_tracker, capsys):
    print_search_results(fake_tracker, [make_result(100)], "artist album")
    out = capsys.readouterr().out
    assert "Results matching this release were found on RED" in out
    assert " 01 >> 100 | " in out
    assert f"{fake_tracker.base_url}/torrents.php?id=100" in out


def test_print_search_results_skips_malformed_results(fake_tracker, capsys):
    malformed = {"groupId": 1}  # missing artist/groupName/etc.
    print_search_results(fake_tracker, [malformed, make_result(100)], "artist album")
    out = capsys.readouterr().out
    assert " 02 >> 100 | " in out  # index preserved for the valid entry
    assert " 01 >> 1 | " not in out  # malformed entry produces no partial row


def test_print_search_results_skips_result_with_unjoinable_tags(fake_tracker, capsys):
    # All keys present but tags is None -> ', '.join raises TypeError. The whole row
    # must be skipped inside the try, not printed partially before the join blows up.
    bad = make_result(1)
    bad["tags"] = None
    print_search_results(fake_tracker, [bad, make_result(100)], "artist album")
    out = capsys.readouterr().out
    assert " 02 >> 100 | " in out
    assert " 01 >> 1 | " not in out


def test_print_recent_upload_results_limits_to_five(fake_tracker, capsys):
    uploads = [(i, f"Artist{i}", f"Album{i}") for i in range(1, 8)]
    print_recent_upload_results(fake_tracker, uploads, "artist album")
    out = capsys.readouterr().out
    assert "Artist5 - Album5" in out
    assert "Artist6" not in out
    assert f"{fake_tracker.base_url}/torrents.php?torrentid=1" in out


def test_print_recent_upload_results_silent_when_empty(fake_tracker, capsys):
    print_recent_upload_results(fake_tracker, [], "artist album")
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# check_existing_group (orchestration)
# ---------------------------------------------------------------------------


async def test_check_existing_group_pick_result_and_confirm(fake_tracker, install_prompt):
    install_prompt("1", "Y")
    fake_tracker.api_responses["browse"] = {"results": [make_result(100)]}
    assert await check_existing_group(fake_tracker, ["artist album"]) == 100


async def test_check_existing_group_confirmation_declined_returns_none(fake_tracker, install_prompt):
    install_prompt("1", "n")
    fake_tracker.api_responses["browse"] = {"results": [make_result(100)]}
    assert await check_existing_group(fake_tracker, ["artist album"]) is None


async def test_check_existing_group_new_group_skips_confirmation(fake_tracker, install_prompt):
    queue = install_prompt("n")
    fake_tracker.api_responses["browse"] = {"results": [make_result(100)]}
    assert await check_existing_group(fake_tracker, ["artist album"]) is None
    assert queue.calls == 1


async def test_check_existing_group_abort_propagates(fake_tracker, install_prompt):
    install_prompt("a")
    fake_tracker.api_responses["browse"] = {"results": [make_result(100)]}
    with pytest.raises(click.Abort):
        await check_existing_group(fake_tracker, ["artist album"])


async def test_check_existing_group_delete_folder_propagates(fake_tracker, install_prompt):
    install_prompt("d")
    fake_tracker.api_responses["browse"] = {"results": [make_result(100)]}
    with pytest.raises(AbortAndDeleteFolder):
        await check_existing_group(fake_tracker, ["artist album"])


async def test_check_existing_group_no_deletion_offer_is_passed_through(fake_tracker, install_prompt):
    queue = install_prompt("d", "n")
    fake_tracker.api_responses["browse"] = {"results": [make_result(100)]}
    assert await check_existing_group(fake_tracker, ["artist album"], offer_deletion=False) is None
    assert queue.calls == 2


async def test_check_existing_group_no_results_uses_recent_log_flow(fake_tracker, install_prompt, monkeypatch):
    monkeypatch.setattr(cfg.upload.requests, "check_recent_uploads", True)
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.5)
    install_prompt("1", "Y")
    fake_tracker.api_responses["browse"] = {"results": []}
    fake_tracker.api_responses["torrentgroup"] = make_torrentgroup_response(777)
    install_log(fake_tracker, [(111, "Artist", "Album")])
    install_redirect(fake_tracker, {"111": 777})
    assert await check_existing_group(fake_tracker, ["artist album"]) == 777


async def test_check_existing_group_no_results_recent_flow_new_group(fake_tracker, install_prompt, monkeypatch):
    monkeypatch.setattr(cfg.upload.requests, "check_recent_uploads", True)
    monkeypatch.setattr(cfg.upload, "log_dupe_tolerance", 0.5)
    queue = install_prompt("n")
    fake_tracker.api_responses["browse"] = {"results": []}
    install_log(fake_tracker, [])
    assert await check_existing_group(fake_tracker, ["artist album"]) is None
    assert queue.calls == 1


async def test_check_existing_group_recent_check_disabled_uses_group_prompt(fake_tracker, install_prompt, monkeypatch):
    monkeypatch.setattr(cfg.upload.requests, "check_recent_uploads", False)
    install_prompt("42", "Y")
    fake_tracker.api_responses["browse"] = {"results": []}
    fake_tracker.api_responses["torrentgroup"] = make_torrentgroup_response(42)
    # get_uploads_from_log intentionally NOT installed: this path must not touch it.
    assert await check_existing_group(fake_tracker, ["artist album"]) == 42


async def test_check_existing_group_zero_input_never_returns_zero(fake_tracker, install_prompt, monkeypatch):
    monkeypatch.setattr(cfg.upload.requests, "check_recent_uploads", False)
    queue = install_prompt("0", "n")
    fake_tracker.api_responses["browse"] = {"results": []}
    assert await check_existing_group(fake_tracker, ["artist album"]) is None
    assert queue.calls == 2


async def test_check_existing_group_yes_all_does_not_skip_prompts(fake_tracker, install_prompt, monkeypatch):
    # dupe_checker has no cfg.upload.yes_all short-circuit: prompts still happen.
    monkeypatch.setattr(cfg.upload, "yes_all", True)
    queue = install_prompt("n")
    fake_tracker.api_responses["browse"] = {"results": [make_result(100)]}
    assert await check_existing_group(fake_tracker, ["artist album"]) is None
    assert queue.calls == 1
