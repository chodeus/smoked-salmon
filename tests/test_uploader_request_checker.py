"""Tests for request filling during an upload.

Covers ``salmon.uploader.request_checker`` (search, interactive selection,
confirmation) plus ``salmon.trackers.validate_request`` and the flow of the
chosen ``request_id`` into the upload payload (``requestid`` key).
"""

from types import SimpleNamespace

import asyncclick as click
import pytest

import salmon.trackers
from salmon import cfg
from salmon.errors import RequestError
from salmon.uploader.request_checker import (
    _confirm_request_id,
    _print_request_details,
    _prompt_for_request_id,
    check_requests,
    get_request_results,
    print_request_results,
)
from salmon.uploader.upload import (
    compile_data_existing_group,
    compile_data_new_group,
    prepare_and_upload,
)


@pytest.fixture(autouse=True)
def _deterministic_cfg(monkeypatch):
    """Pin the cfg flags the module branches on, regardless of dev config."""
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload.requests, "always_ask_for_request_fill", False)


def patch_prompt(monkeypatch, answers):
    """Replace click.prompt in the module under test with a scripted fake.

    Returns the list of recorded calls so tests can assert on prompt usage.
    """
    remaining = list(answers)
    calls = []

    async def fake_prompt(*args, **kwargs):
        calls.append((args, kwargs))
        assert remaining, "click.prompt called more often than answers were scripted"
        return remaining.pop(0)

    monkeypatch.setattr("salmon.uploader.request_checker.click.prompt", fake_prompt)
    return calls


def make_search_result(request_id=101, **overrides):
    """A request dict as returned by the ajax 'requests' search action."""
    result = {
        "requestId": request_id,
        "categoryName": "Music",
        "artists": [[{"name": "Testartist"}], []],
        "title": "Testalbum",
        "year": 2024,
        "releaseType": "Album",
        "bitrateList": ["Lossless"],
        "formatList": ["FLAC"],
        "mediaList": ["WEB"],
    }
    result.update(overrides)
    return result


def make_request_detail(request_id=101, **overrides):
    """A request dict as returned by the ajax 'request' detail action."""
    detail = {
        "requestId": request_id,
        "title": "Testalbum",
        "year": 2024,
        "requestorName": "requestor",
        "totalBounty": 1073741824,
        "bitrateList": ["Lossless"],
        "formatList": ["FLAC"],
        "mediaList": ["WEB"],
        "bbDescription": "A fine request",
        "musicInfo": {"artists": [{"name": "Testartist"}]},
    }
    detail.update(overrides)
    return detail


# ---------------------------------------------------------------------------
# validate_request (salmon.trackers.validate_request)
# ---------------------------------------------------------------------------


def test_validate_request_none_returns_none(fake_tracker):
    assert salmon.trackers.validate_request(fake_tracker, None) is None


def test_validate_request_numeric_string_returned_unchanged_as_string(fake_tracker):
    # Numeric ids stay strings; they are not converted to int.
    assert salmon.trackers.validate_request(fake_tracker, "1234") == "1234"


def test_validate_request_numeric_with_whitespace_returned_stripped(fake_tracker):
    assert salmon.trackers.validate_request(fake_tracker, " 1234 ") == "1234"


def test_validate_request_full_url_extracts_id(fake_tracker):
    url = f"{fake_tracker.base_url}/requests.php?action=view&id=5678"
    assert salmon.trackers.validate_request(fake_tracker, url) == "5678"


def test_validate_request_url_without_id_param_raises_bad_parameter(fake_tracker):
    url = f"{fake_tracker.base_url}/requests.php?action=view"
    with pytest.raises(click.BadParameter):
        salmon.trackers.validate_request(fake_tracker, url)


def test_validate_request_non_string_raises_bad_parameter(fake_tracker):
    # An int has no .strip() -> AttributeError -> BadParameter.
    with pytest.raises(click.BadParameter):
        salmon.trackers.validate_request(fake_tracker, 1234)


def test_validate_request_garbage_string_raises_bad_parameter(fake_tracker):
    with pytest.raises(click.BadParameter):
        salmon.trackers.validate_request(fake_tracker, "garbage")


def test_validate_request_foreign_tracker_url_raises_bad_parameter(fake_tracker):
    url = "https://orpheus.network/requests.php?action=view&id=99"
    with pytest.raises(click.BadParameter):
        salmon.trackers.validate_request(fake_tracker, url)


# ---------------------------------------------------------------------------
# get_request_results
# ---------------------------------------------------------------------------


async def test_get_request_results_returns_music_requests(fake_tracker):
    fake_tracker.api_responses["requests"] = {"results": [make_search_result(101)]}

    results = await get_request_results(fake_tracker, ["Testartist Testalbum"])

    assert [r["requestId"] for r in results] == [101]
    assert fake_tracker.api_calls == [("requests", {"search": "Testartist Testalbum"})]


async def test_get_request_results_filters_non_music_categories(fake_tracker):
    fake_tracker.api_responses["requests"] = {
        "results": [
            make_search_result(101),
            make_search_result(202, categoryName="Applications"),
            make_search_result(303, categoryName="E-Books"),
        ]
    }

    results = await get_request_results(fake_tracker, ["Testalbum"])

    assert [r["requestId"] for r in results] == [101]


async def test_get_request_results_empty_results_returns_empty_list(fake_tracker):
    fake_tracker.api_responses["requests"] = {"results": []}

    assert await get_request_results(fake_tracker, ["Testalbum"]) == []


async def test_get_request_results_queries_once_per_searchstr_and_dedupes(fake_tracker):
    fake_tracker.api_responses["requests"] = {"results": [make_search_result(101)]}

    results = await get_request_results(fake_tracker, ["Testalbum", "Testartist"])

    # Same dict returned for both searches is only kept once.
    assert [r["requestId"] for r in results] == [101]
    assert len(fake_tracker.api_calls) == 2
    assert fake_tracker.api_calls[0] == ("requests", {"search": "Testalbum"})
    assert fake_tracker.api_calls[1] == ("requests", {"search": "Testartist"})


async def test_get_request_results_merges_distinct_results_across_searchstrs(fake_tracker):
    responses = {
        "Testalbum": {"results": [make_search_result(101)]},
        "Testartist": {"results": [make_search_result(202)]},
    }

    async def api_call(action, params):
        return responses[params["search"]]

    fake_tracker.api_call = api_call

    results = await get_request_results(fake_tracker, ["Testalbum", "Testartist"])

    assert [r["requestId"] for r in results] == [101, 202]


async def test_get_request_results_api_error_propagates(fake_tracker):
    fake_tracker.api_responses["requests"] = RequestError("failed to get request results")

    with pytest.raises(RequestError):
        await get_request_results(fake_tracker, ["Testalbum"])


async def test_get_request_results_does_not_filter_on_format_bitrate_or_media(fake_tracker):
    # Pin: the module does NOT match requests against the release being
    # uploaded. A request that only allows MP3/V0/Vinyl is still returned for
    # a FLAC/WEB upload; format/bitrate/media lists are display-only.
    mismatching = make_search_result(
        404,
        formatList=["MP3"],
        bitrateList=["V0 (VBR)"],
        mediaList=["Vinyl"],
    )
    fake_tracker.api_responses["requests"] = {"results": [mismatching]}

    results = await get_request_results(fake_tracker, ["Testalbum"])

    assert [r["requestId"] for r in results] == [404]


# ---------------------------------------------------------------------------
# print_request_results
# ---------------------------------------------------------------------------


def test_print_request_results_no_results_message(fake_tracker, capsys):
    print_request_results(fake_tracker, [], "Testartist Testalbum")

    out = capsys.readouterr().out
    assert "No requests were found on RED" in out
    assert "(searchstrs: Testartist Testalbum)" in out


def test_print_request_results_lists_numbered_urls_and_requirements(fake_tracker, capsys):
    results = [make_search_result(101), make_search_result(202)]

    print_request_results(fake_tracker, results, "Testalbum")

    out = capsys.readouterr().out
    assert "Requests were found on RED" in out
    assert f" 01 >> {fake_tracker.base_url}/requests.php?action=view&id=101 | " in out
    assert f" 02 >> {fake_tracker.base_url}/requests.php?action=view&id=202 | " in out
    assert "Testartist" in out
    assert "- Testalbum" in out
    assert "(2024) [Album]" in out
    assert "Requirements: Lossless / " in out


def test_print_request_results_skips_malformed_row_without_partial_output(fake_tracker, capsys):
    # bitrateList=None raises on join: the row must be skipped whole, not printed
    # partially with no trailing newline (which would garble the next row onto it).
    bad = make_search_result(101, bitrateList=None)
    print_request_results(fake_tracker, [bad, make_search_result(202)], "Testalbum")
    out = capsys.readouterr().out
    assert " 01 >> " not in out
    assert f" 02 >> {fake_tracker.base_url}/requests.php?action=view&id=202 | " in out


def test_print_request_results_more_than_three_artists_shows_various(fake_tracker, capsys):
    artists = [[{"name": f"Artist{i}"} for i in range(4)], []]
    results = [make_search_result(101, artists=artists)]

    print_request_results(fake_tracker, results, "Testalbum")

    out = capsys.readouterr().out
    assert "Various Artists" in out
    assert "Artist0" not in out


def test_print_request_results_joins_up_to_three_artist_names(fake_tracker, capsys):
    artists = [[{"name": "Artist A"}, {"name": "Artist B"}], []]
    results = [make_search_result(101, artists=artists)]

    print_request_results(fake_tracker, results, "Testalbum")

    assert "Artist A Artist B " in capsys.readouterr().out


def test_print_request_results_skips_malformed_entries(fake_tracker, capsys):
    # First result lacks requestId -> KeyError -> silently skipped; the good
    # one still prints (keeping its enumeration index).
    results = [{"categoryName": "Music"}, make_search_result(202)]

    print_request_results(fake_tracker, results, "Testalbum")

    out = capsys.readouterr().out
    assert " 01 >>" not in out
    assert f" 02 >> {fake_tracker.base_url}/requests.php?action=view&id=202 | " in out


# ---------------------------------------------------------------------------
# _prompt_for_request_id
# ---------------------------------------------------------------------------


async def test_prompt_pick_result_by_number_returns_its_request_id(fake_tracker, monkeypatch):
    patch_prompt(monkeypatch, ["2"])
    results = [make_search_result(101), make_search_result(202)]

    assert await _prompt_for_request_id(fake_tracker, results) == 202


async def test_prompt_zero_is_clamped_to_first_result(fake_tracker, monkeypatch):
    # Pin: "0" is clamped to index 0 and silently selects the first result.
    patch_prompt(monkeypatch, ["0"])
    results = [make_search_result(101), make_search_result(202)]

    assert await _prompt_for_request_id(fake_tracker, results) == 101


async def test_prompt_number_beyond_results_is_treated_as_raw_request_id(fake_tracker, monkeypatch, capsys):
    patch_prompt(monkeypatch, ["7"])
    results = [make_search_result(101), make_search_result(202)]

    assert await _prompt_for_request_id(fake_tracker, results) == 7
    assert "Interpreting 7 as a request id" in capsys.readouterr().out


async def test_prompt_digit_with_no_results_is_treated_as_request_id(fake_tracker, monkeypatch):
    patch_prompt(monkeypatch, ["123456"])

    assert await _prompt_for_request_id(fake_tracker, []) == 123456


async def test_prompt_pasted_request_url_returns_extracted_id(fake_tracker, monkeypatch):
    url = f"{fake_tracker.base_url}/requests.php?action=view&id=777"
    patch_prompt(monkeypatch, [url])

    assert await _prompt_for_request_id(fake_tracker, []) == 777


async def test_prompt_pasted_url_without_id_param_reprompts(fake_tracker, monkeypatch):
    calls = patch_prompt(monkeypatch, [f"{fake_tracker.base_url}/requests.php?action=view", "n"])

    assert await _prompt_for_request_id(fake_tracker, []) is None
    assert len(calls) == 2


@pytest.mark.parametrize("answer", ["n", "N", "No", "nope", "", "   "])
async def test_prompt_decline_returns_none(fake_tracker, monkeypatch, answer, capsys):
    patch_prompt(monkeypatch, [answer])

    assert await _prompt_for_request_id(fake_tracker, [make_search_result(101)]) is None
    assert "Not filling a request" in capsys.readouterr().out


async def test_prompt_invalid_input_reprompts_until_valid(fake_tracker, monkeypatch):
    foreign_url = "https://orpheus.network/requests.php?action=view&id=5"
    calls = patch_prompt(monkeypatch, ["garbage", foreign_url, "n"])

    assert await _prompt_for_request_id(fake_tracker, [make_search_result(101)]) is None
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# _confirm_request_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["y", "Y", "yes"])
async def test_confirm_yes_returns_true(fake_tracker, monkeypatch, answer):
    fake_tracker.api_responses["request"] = make_request_detail(101)
    patch_prompt(monkeypatch, [answer])

    assert await _confirm_request_id(fake_tracker, 101) is True
    assert fake_tracker.api_calls == [("request", {"id": 101})]


@pytest.mark.parametrize("answer", ["n", "No"])
async def test_confirm_no_returns_false(fake_tracker, monkeypatch, answer, capsys):
    fake_tracker.api_responses["request"] = make_request_detail(101)
    patch_prompt(monkeypatch, [answer])

    assert await _confirm_request_id(fake_tracker, 101) is False
    assert "Not filling this request" in capsys.readouterr().out


async def test_confirm_invalid_answer_reprompts(fake_tracker, monkeypatch):
    fake_tracker.api_responses["request"] = make_request_detail(101)
    calls = patch_prompt(monkeypatch, ["x", "maybe", "y"])

    assert await _confirm_request_id(fake_tracker, 101) is True
    assert len(calls) == 3


async def test_confirm_yes_all_skips_prompt(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload, "yes_all", True)
    fake_tracker.api_responses["request"] = make_request_detail(101)
    calls = patch_prompt(monkeypatch, [])

    assert await _confirm_request_id(fake_tracker, 101) is True
    assert calls == []


async def test_confirm_nonexistent_request_aborts(fake_tracker, monkeypatch, capsys):
    fake_tracker.api_responses["request"] = RequestError("request not found")
    patch_prompt(monkeypatch, [])

    with pytest.raises(click.Abort):
        await _confirm_request_id(fake_tracker, 424242)
    assert "424242 does not exist." in capsys.readouterr().out


async def test_confirm_more_than_three_artists_shows_various(fake_tracker, monkeypatch, capsys):
    detail = make_request_detail(101, musicInfo={"artists": [{"name": f"Artist{i}"} for i in range(4)]})
    fake_tracker.api_responses["request"] = detail
    patch_prompt(monkeypatch, ["y"])

    assert await _confirm_request_id(fake_tracker, 101) is True
    assert "Various Artists" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _print_request_details
# ---------------------------------------------------------------------------


def test_print_details_shows_bounty_and_requirements(fake_tracker, capsys):
    req = make_request_detail(101, artist="Testartist ")

    _print_request_details(fake_tracker, req)

    out = capsys.readouterr().out
    assert f"{fake_tracker.base_url}/requests.php?action=view&id=101" in out
    assert "1 GiB" in out
    assert "Allowed Bitrate: Lossless" in out
    assert "Allowed Formats: FLAC" in out
    assert "Allowed   Media: WEB" in out
    assert "A fine request" in out


def test_print_details_falls_back_to_bounty_key(fake_tracker, capsys):
    req = make_request_detail(101, artist="Testartist ")
    del req["totalBounty"]
    req["bounty"] = 1048576

    _print_request_details(fake_tracker, req)

    assert "1 MiB" in capsys.readouterr().out


def test_print_details_red_style_cd_uses_preformatted_log_cue(fake_tracker, capsys):
    req = make_request_detail(101, artist="Testartist ", mediaList=["CD", "WEB"], logCue="Log (100%) + Cue")

    _print_request_details(fake_tracker, req)

    assert "Allowed   Media: WEB | CD Log (100%) + Cue" in capsys.readouterr().out


def test_print_details_ops_style_cd_builds_requirements_from_fields(fake_tracker, capsys):
    req = make_request_detail(
        101,
        artist="Testartist ",
        mediaList=["CD"],
        needLog=True,
        minLogScore=90,
        needCue=True,
        needLogChecksum=True,
    )

    _print_request_details(fake_tracker, req)

    assert "Allowed   Media: CD + Log (90%) + Cue + Checksum" in capsys.readouterr().out


def test_print_details_truncates_long_description(fake_tracker, capsys):
    description = "\n".join(f"line{i}" for i in range(1, 8))
    req = make_request_detail(101, artist="Testartist ", bbDescription=description)

    _print_request_details(fake_tracker, req)

    out = capsys.readouterr().out
    assert "line5" in out
    assert "...2 more lines..." in out
    assert "line6" not in out


# ---------------------------------------------------------------------------
# check_requests (full interactive flow)
# ---------------------------------------------------------------------------


async def test_check_requests_pick_and_confirm_returns_request_id(fake_tracker, monkeypatch):
    fake_tracker.api_responses["requests"] = {"results": [make_search_result(101)]}
    fake_tracker.api_responses["request"] = make_request_detail(101)
    patch_prompt(monkeypatch, ["1", "y"])

    assert await check_requests(fake_tracker, ["Testartist Testalbum"]) == 101
    assert fake_tracker.api_calls == [
        ("requests", {"search": "Testartist Testalbum"}),
        ("request", {"id": 101}),
    ]


async def test_check_requests_decline_at_selection_returns_none(fake_tracker, monkeypatch):
    fake_tracker.api_responses["requests"] = {"results": [make_search_result(101)]}
    calls = patch_prompt(monkeypatch, ["n"])

    assert await check_requests(fake_tracker, ["Testalbum"]) is None
    # The detail/confirmation step is never reached.
    assert len(calls) == 1
    assert [action for action, _ in fake_tracker.api_calls] == ["requests"]


async def test_check_requests_confirm_no_returns_none(fake_tracker, monkeypatch):
    fake_tracker.api_responses["requests"] = {"results": [make_search_result(101)]}
    fake_tracker.api_responses["request"] = make_request_detail(101)
    patch_prompt(monkeypatch, ["1", "n"])

    assert await check_requests(fake_tracker, ["Testalbum"]) is None


async def test_check_requests_no_results_returns_none_without_prompting(fake_tracker, monkeypatch):
    fake_tracker.api_responses["requests"] = {"results": []}
    calls = patch_prompt(monkeypatch, [])

    assert await check_requests(fake_tracker, ["Testalbum"]) is None
    assert calls == []


async def test_check_requests_no_results_with_always_ask_prompts_anyway(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload.requests, "always_ask_for_request_fill", True)
    fake_tracker.api_responses["requests"] = {"results": []}
    fake_tracker.api_responses["request"] = make_request_detail(555)
    url = f"{fake_tracker.base_url}/requests.php?action=view&id=555"
    patch_prompt(monkeypatch, [url, "y"])

    assert await check_requests(fake_tracker, ["Testalbum"]) == 555


async def test_check_requests_yes_all_confirms_without_second_prompt(fake_tracker, monkeypatch):
    monkeypatch.setattr(cfg.upload, "yes_all", True)
    fake_tracker.api_responses["requests"] = {"results": [make_search_result(101)]}
    fake_tracker.api_responses["request"] = make_request_detail(101)
    calls = patch_prompt(monkeypatch, ["1"])

    assert await check_requests(fake_tracker, ["Testalbum"]) == 101
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# request_id in the upload payload
# ---------------------------------------------------------------------------


def _minimal_existing_group_metadata():
    return {
        "year": 2024,
        "edition_title": "",
        "label": "Testlabel",
        "catno": "CAT-001",
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "source": "WEB",
    }


def test_compile_data_existing_group_includes_requestid(fake_tracker):
    data = compile_data_existing_group(
        fake_tracker,
        "/fake/path",
        999,
        _minimal_existing_group_metadata(),
        track_data={},
        hybrid=False,
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=None,
        request_id=4321,
        override_description="desc",
    )

    assert data["requestid"] == 4321
    assert data["groupid"] == 999
    assert data["submit"] is True


def test_compile_data_new_group_includes_requestid(fake_tracker):
    fake_tracker.release_types = {"Album": 1}
    metadata = {
        **_minimal_existing_group_metadata(),
        "title": "Testalbum",
        "artists": [("Testartist", "main")],
        "group_year": 2024,
        "rls_type": "Album",
        "tags": "electronic",
        "comment": None,
        "urls": [],
        "date": "2024-01-01",
    }
    track_data = {
        "01. Intro.flac": {
            "duration": 61,
            "sample rate": 44100,
            "precision": 16,
            "bit rate": 1000000,
            "t": SimpleNamespace(discnumber="1/1", tracknumber="1", artist=["Testartist"], title="Intro"),
        }
    }

    data = compile_data_new_group(
        fake_tracker,
        "/fake/path",
        metadata,
        track_data,
        hybrid=False,
        cover_url="https://example.invalid/cover.jpg",
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=None,
        request_id=42,
    )

    assert data["requestid"] == 42
    assert data["title"] == "Testalbum"
    assert data["artists[]"] == ["Testartist"]
    assert "groupid" not in data


async def test_prepare_and_upload_sends_requestid_to_tracker(fake_tracker, album_dir, tmp_path):
    torrents_dir = tmp_path / "torrents"
    torrents_dir.mkdir()
    fake_tracker.dot_torrents_dir = str(torrents_dir)

    async def ensure_authenticated():
        return None

    fake_tracker.ensure_authenticated = ensure_authenticated

    torrent_id, group_id, torrent_path, _torrent = await prepare_and_upload(
        fake_tracker,
        str(album_dir),
        group_id=999,
        metadata=_minimal_existing_group_metadata(),
        cover_url=None,
        track_data={},
        hybrid=False,
        lossy_master=False,
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=None,
        request_id=4321,
        override_description="desc",
    )

    assert (torrent_id, group_id) == (1001, 2002)
    assert len(fake_tracker.uploads) == 1
    data, files = fake_tracker.uploads[0]
    assert data["requestid"] == 4321
    assert data["groupid"] == 999
    assert files.torrent_data  # the generated .torrent is part of the payload
    assert torrent_path == str(torrents_dir / f"{album_dir.name} - RED.torrent")
    assert (torrents_dir / f"{album_dir.name} - RED.torrent").exists()
