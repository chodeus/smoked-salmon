"""Tests for site-specific tracker behavior and tracker selection.

Covers src/salmon/trackers/{__init__,red,ops,dic}.py:
- OPS split-release prompt and OPS-specific overrides
- DIC buy/diy/exclusive mark prompts
- RED upload dispatch (log files force site page upload) and group-page enrichment
- get_class / choose_tracker / validate_tracker / validate_request
"""

import sys
import types
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncclick as click  # noqa: E402

import salmon.trackers as trackers  # noqa: E402
from salmon import cfg  # noqa: E402
from salmon.common import UploadFiles  # noqa: E402
from salmon.config.validations import GazelleTrackerSettings  # noqa: E402
from salmon.constants import RELEASE_TYPES  # noqa: E402
from salmon.errors import RequestError  # noqa: E402
from salmon.trackers import dic, ops, red  # noqa: E402
from salmon.trackers.base import BaseGazelleApi, HttpResponse  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def upload_files() -> UploadFiles:
    return UploadFiles(torrent_data=b"torrent")


@pytest.fixture
def base_uploads(monkeypatch) -> list[tuple[dict, UploadFiles]]:
    """Record calls that reach BaseGazelleApi.upload; return (123, 456)."""
    calls: list[tuple[dict, UploadFiles]] = []

    async def fake_upload(self, data, files):
        calls.append((data, files))
        return (123, 456)

    monkeypatch.setattr(BaseGazelleApi, "upload", fake_upload)
    return calls


@pytest.fixture
def ops_tracker(monkeypatch) -> ops.OpsApi:
    monkeypatch.setattr(cfg.tracker, "ops", GazelleTrackerSettings(session="ops-session"))
    return ops.OpsApi()


@pytest.fixture
def dic_tracker(monkeypatch) -> dic.DICApi:
    monkeypatch.setattr(cfg.tracker, "dic", GazelleTrackerSettings(session="dic-session"))
    return dic.DICApi()


@pytest.fixture
def red_tracker(monkeypatch) -> red.RedApi:
    monkeypatch.setattr(cfg.tracker, "red", GazelleTrackerSettings(session="red-session"))
    tracker = red.RedApi()
    tracker.api_key = ""  # deterministic regardless of developer config
    return tracker


def patch_ops_confirm(monkeypatch, answer: bool) -> list[tuple[tuple, dict]]:
    """Patch the (synchronous) asyncclick confirm used in ops.py, recording calls."""
    calls: list[tuple[tuple, dict]] = []

    def fake_confirm(*args, **kwargs):
        calls.append((args, kwargs))
        return answer

    monkeypatch.setattr("salmon.trackers.ops.click.confirm", fake_confirm)
    return calls


def patch_dic_prompts(monkeypatch, answers: list[str]) -> list[str]:
    """Patch the awaited asyncclick prompt used in dic.py with canned answers."""
    seen: list[str] = []
    queue = list(answers)

    async def fake_prompt(message, *args, **kwargs):
        seen.append(str(message))
        return queue.pop(0)

    monkeypatch.setattr("salmon.trackers.dic.click.prompt", fake_prompt)
    return seen


def patch_tracker_prompt(monkeypatch, answers: list[str]) -> list[dict]:
    """Patch the awaited asyncclick prompt used in trackers/__init__.py."""
    seen: list[dict] = []
    queue = list(answers)

    async def fake_prompt(message, *args, **kwargs):
        seen.append(kwargs)
        return queue.pop(0)

    monkeypatch.setattr("salmon.trackers.click.prompt", fake_prompt)
    return seen


# ---------------------------------------------------------------------------
# OPS: constructor and release types
# ---------------------------------------------------------------------------


def test_ops_constructor_pins_site_identity_and_cookie(ops_tracker) -> None:
    assert ops_tracker.site_code == "OPS"
    assert ops_tracker.base_url == "https://orpheus.network"
    assert ops_tracker.tracker_url == "https://home.opsfet.ch"
    assert ops_tracker.cookie == "ops-session"
    assert ops_tracker.api_key == ""
    assert ops_tracker._split_prompted is False
    assert ops_tracker._use_split is False


def test_ops_release_types_differ_from_base_gazelle_values(ops_tracker) -> None:
    # OPS-specific ids; notably Split exists only on OPS.
    assert ops_tracker.release_types["Single"] == 9
    assert ops_tracker.release_types["Split"] == 12
    assert ops_tracker.release_types["Demo"] == 10
    assert ops_tracker.release_types["DJ Mix"] == 17
    assert "Split" not in RELEASE_TYPES
    assert RELEASE_TYPES["Demo"] == 17
    assert RELEASE_TYPES["DJ Mix"] == 19


# ---------------------------------------------------------------------------
# OPS: split prompt behavior
# ---------------------------------------------------------------------------


async def test_ops_split_prompt_two_mains_single_yes_sets_split(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    confirms = patch_ops_confirm(monkeypatch, answer=True)

    result = await ops_tracker.upload(
        {
            "releasetype": ops_tracker.release_types["Single"],
            "artists[]": ["A", "B"],
            "importance[]": [1, 1],
        },
        upload_files,
    )

    assert result == (123, 456)
    assert len(confirms) == 1
    assert confirms[0][1].get("default") is False
    uploaded_data, uploaded_files = base_uploads[0]
    assert uploaded_data["releasetype"] == 12  # Split
    assert uploaded_files is upload_files


async def test_ops_split_prompt_two_mains_single_no_keeps_releasetype(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    patch_ops_confirm(monkeypatch, answer=False)

    await ops_tracker.upload(
        {"releasetype": 9, "importance[]": [1, 1]},
        upload_files,
    )

    assert base_uploads[0][0]["releasetype"] == 9


async def test_ops_split_prompt_yes_answer_cached_across_uploads(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    confirms = patch_ops_confirm(monkeypatch, answer=True)

    await ops_tracker.upload({"releasetype": 9, "importance[]": [1, 1]}, upload_files)
    await ops_tracker.upload({"releasetype": 9, "importance[]": [1, 1]}, upload_files)

    assert len(confirms) == 1  # asked only once per instance
    assert [call[0]["releasetype"] for call in base_uploads] == [12, 12]


async def test_ops_split_prompt_no_answer_cached_across_uploads(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    confirms = patch_ops_confirm(monkeypatch, answer=False)

    await ops_tracker.upload({"releasetype": 9, "importance[]": [1, 1]}, upload_files)
    await ops_tracker.upload({"releasetype": 9, "importance[]": [1, 1]}, upload_files)

    assert len(confirms) == 1
    assert [call[0]["releasetype"] for call in base_uploads] == [9, 9]


async def test_ops_split_prompt_not_asked_without_releasetype(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    # Uploading into an existing group has no 'releasetype' key.
    confirms = patch_ops_confirm(monkeypatch, answer=True)
    data = {"groupid": 42, "importance[]": [1, 1]}

    await ops_tracker.upload(data, upload_files)

    assert confirms == []
    assert base_uploads[0][0] == data
    assert "releasetype" not in base_uploads[0][0]


async def test_ops_split_prompt_not_asked_for_single_main_artist(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    confirms = patch_ops_confirm(monkeypatch, answer=True)

    await ops_tracker.upload({"releasetype": 9, "importance[]": [1]}, upload_files)

    assert confirms == []
    assert base_uploads[0][0]["releasetype"] == 9


async def test_ops_split_prompt_not_asked_without_importance_list(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    confirms = patch_ops_confirm(monkeypatch, answer=True)

    await ops_tracker.upload({"releasetype": 9}, upload_files)

    assert confirms == []
    assert base_uploads[0][0]["releasetype"] == 9


async def test_ops_split_prompt_also_asked_for_non_single_releasetype(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    # Pin actual behavior: the prompt is NOT limited to Single releases —
    # any new-group upload with >= 2 main artists triggers it (here: Album).
    confirms = patch_ops_confirm(monkeypatch, answer=True)

    await ops_tracker.upload(
        {"releasetype": ops_tracker.release_types["Album"], "importance[]": [1, 1]},
        upload_files,
    )

    assert len(confirms) == 1
    assert base_uploads[0][0]["releasetype"] == 12


async def test_ops_split_prompt_counts_string_importance_values(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    confirms = patch_ops_confirm(monkeypatch, answer=True)

    await ops_tracker.upload({"releasetype": 9, "importance[]": ["1", " 1 ", "2"]}, upload_files)

    assert len(confirms) == 1
    assert base_uploads[0][0]["releasetype"] == 12


async def test_ops_cached_split_yes_does_not_add_releasetype_when_absent(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    patch_ops_confirm(monkeypatch, answer=True)

    await ops_tracker.upload({"releasetype": 9, "importance[]": [1, 1]}, upload_files)
    await ops_tracker.upload({"groupid": 7, "importance[]": [1, 1]}, upload_files)

    assert base_uploads[0][0]["releasetype"] == 12
    assert "releasetype" not in base_uploads[1][0]


async def test_ops_split_replacement_does_not_mutate_caller_data(
    ops_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    patch_ops_confirm(monkeypatch, answer=True)
    data = {"releasetype": 9, "importance[]": [1, 1]}

    await ops_tracker.upload(data, upload_files)

    assert data["releasetype"] == 9  # caller dict untouched
    assert base_uploads[0][0]["releasetype"] == 12


# ---------------------------------------------------------------------------
# OPS: group page parsing and lossy master reporting
# ---------------------------------------------------------------------------


def test_ops_parse_group_page_returns_ids_of_newest_permalink(ops_tracker) -> None:
    html = """
    <html><body>
    <a title="Permalink" href="torrents.php?id=11&torrentid=100">PL</a>
    <a title="Permalink" href="torrents.php?id=12&torrentid=205">PL</a>
    <a href="torrents.php?id=99&torrentid=999">not a permalink</a>
    </body></html>
    """

    torrent_id, group_id = ops_tracker.parse_most_recent_torrent_and_group_id_from_group_page(html)

    assert (torrent_id, group_id) == (205, 12)


def test_ops_parse_group_page_without_permalinks_raises_typeerror(ops_tracker) -> None:
    with pytest.raises(TypeError, match="no permalink ids found"):
        ops_tracker.parse_most_recent_torrent_and_group_id_from_group_page("<html></html>")


async def test_ops_report_lossy_master_always_uses_lossyapproval(ops_tracker, monkeypatch) -> None:
    # OPS has no lossywebapproval report type, even for WEB sources.
    captured: dict = {}
    ops_tracker.authkey = "ops-auth"

    async def fake_request(method, url, params=None, data=None, **kwargs):
        captured.update(method=method, url=url, params=params, data=data)
        return HttpResponse(text="", url="https://orpheus.network/torrents.php?id=1", status=200)

    monkeypatch.setattr(ops_tracker, "_request", fake_request)

    assert await ops_tracker.report_lossy_master(555, "lossy comment", source="WEB") is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://orpheus.network/reportsv2.php"
    assert captured["params"] == {"action": "takereport"}
    assert captured["data"]["type"] == "lossyapproval"
    assert captured["data"]["torrentid"] == 555
    assert captured["data"]["extra"] == "lossy comment"
    assert captured["data"]["auth"] == "ops-auth"


async def test_ops_report_lossy_master_unexpected_redirect_raises(ops_tracker, monkeypatch) -> None:
    async def fake_request(method, url, params=None, data=None, **kwargs):
        return HttpResponse(text="", url="https://orpheus.network/login.php", status=200)

    monkeypatch.setattr(ops_tracker, "_request", fake_request)

    with pytest.raises(RequestError, match="unexpected redirect"):
        await ops_tracker.report_lossy_master(555, "comment", source="CD")


# ---------------------------------------------------------------------------
# DIC: constructor and mark prompts
# ---------------------------------------------------------------------------


def test_dic_constructor_pins_site_identity_and_cookie(dic_tracker) -> None:
    assert dic_tracker.site_code == "DIC"
    assert dic_tracker.base_url == "https://dicmusic.com"
    assert dic_tracker.tracker_url == "https://tracker.52dic.vip"
    assert dic_tracker.site_string == "DICMusic"
    assert dic_tracker.cookie == "dic-session"
    assert dic_tracker._marks_prompted is False
    assert dic_tracker.specific_params == {}


async def test_dic_purchased_and_exclusive_marks_sent(dic_tracker, base_uploads, upload_files, monkeypatch) -> None:
    prompts = patch_dic_prompts(monkeypatch, ["p", "e"])

    result = await dic_tracker.upload({"title": "Album"}, upload_files)

    assert result == (123, 456)
    assert len(prompts) == 2
    data = base_uploads[0][0]
    assert data["buy"] == "on"
    assert data["jinzhuan"] == "on"
    assert "diy" not in data
    assert data["title"] == "Album"  # original fields preserved


async def test_dic_purchased_without_exclusive_sends_only_buy(
    dic_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    patch_dic_prompts(monkeypatch, ["p", "n"])

    await dic_tracker.upload({}, upload_files)

    assert base_uploads[0][0] == {"buy": "on"}


async def test_dic_selfrip_and_exclusive_marks_sent(dic_tracker, base_uploads, upload_files, monkeypatch) -> None:
    patch_dic_prompts(monkeypatch, ["r", "e"])

    await dic_tracker.upload({}, upload_files)

    data = base_uploads[0][0]
    assert data == {"diy": "on", "jinzhuan": "on"}
    assert "buy" not in data


async def test_dic_selfrip_without_exclusive_sends_only_diy(
    dic_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    patch_dic_prompts(monkeypatch, ["r", "N"])

    await dic_tracker.upload({}, upload_files)

    assert base_uploads[0][0] == {"diy": "on"}


async def test_dic_no_mark_skips_exclusive_prompt(dic_tracker, base_uploads, upload_files, monkeypatch) -> None:
    prompts = patch_dic_prompts(monkeypatch, ["n"])

    await dic_tracker.upload({"title": "Album"}, upload_files)

    assert len(prompts) == 1  # exclusive prompt never asked
    assert base_uploads[0][0] == {"title": "Album"}


async def test_dic_full_word_answers_use_first_letter(dic_tracker, base_uploads, upload_files, monkeypatch) -> None:
    patch_dic_prompts(monkeypatch, ["Purchased", "Exclusive"])

    await dic_tracker.upload({}, upload_files)

    assert base_uploads[0][0] == {"buy": "on", "jinzhuan": "on"}


async def test_dic_empty_answer_treated_as_none(dic_tracker, base_uploads, upload_files, monkeypatch) -> None:
    prompts = patch_dic_prompts(monkeypatch, [""])

    await dic_tracker.upload({}, upload_files)

    assert len(prompts) == 1
    assert base_uploads[0][0] == {}


async def test_dic_marks_prompted_once_and_cached_across_uploads(
    dic_tracker, base_uploads, upload_files, monkeypatch
) -> None:
    prompts = patch_dic_prompts(monkeypatch, ["p", "e"])

    await dic_tracker.upload({"a": 1}, upload_files)
    await dic_tracker.upload({"b": 2}, upload_files)

    assert len(prompts) == 2  # both prompts belong to the first upload
    assert dic_tracker._marks_prompted is True
    assert dic_tracker.specific_params == {"buy": "on", "jinzhuan": "on"}
    assert base_uploads[0][0] == {"a": 1, "buy": "on", "jinzhuan": "on"}
    assert base_uploads[1][0] == {"b": 2, "buy": "on", "jinzhuan": "on"}


async def test_dic_enrichment_does_not_mutate_caller_data(dic_tracker, base_uploads, upload_files, monkeypatch) -> None:
    patch_dic_prompts(monkeypatch, ["p", "n"])
    data = {"title": "Album"}

    await dic_tracker.upload(data, upload_files)

    assert data == {"title": "Album"}
    assert base_uploads[0][0] == {"title": "Album", "buy": "on"}


# ---------------------------------------------------------------------------
# RED: constructor and upload dispatch
# ---------------------------------------------------------------------------


def test_red_constructor_reads_cfg_values(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        cfg.tracker,
        "red",
        GazelleTrackerSettings(session="red-sess", api_key="red-key", dottorrents_dir=str(tmp_path)),
    )

    tracker = red.RedApi()

    assert tracker.site_code == "RED"
    assert tracker.base_url == "https://redacted.sh"
    assert tracker.tracker_url == "https://flacsfor.me"
    assert tracker.cookie == "red-sess"
    assert tracker.api_key == "red-key"
    assert tracker.dot_torrents_dir == str(tmp_path)
    assert tracker._get_cookies() == {"session": "red-sess"}


def test_red_constructor_falls_back_to_global_dottorrents_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cfg.tracker, "red", GazelleTrackerSettings(session="red-sess"))
    monkeypatch.setattr(cfg.directory, "dottorrents_dir", str(tmp_path))

    tracker = red.RedApi()

    assert tracker.dot_torrents_dir == str(tmp_path)


@pytest.fixture
def red_upload_paths(monkeypatch) -> list[tuple[str, dict]]:
    """Record which base upload path (api vs site page) is taken."""
    calls: list[tuple[str, dict]] = []

    async def fake_api_key_upload(self, data, files):
        calls.append(("api", data))
        return (1, 2)

    async def fake_site_page_upload(self, data, files):
        calls.append(("site", data))
        return (3, 4)

    monkeypatch.setattr(BaseGazelleApi, "api_key_upload", fake_api_key_upload)
    monkeypatch.setattr(BaseGazelleApi, "site_page_upload", fake_site_page_upload)
    return calls


async def test_red_upload_with_log_files_forces_site_page_upload_despite_api_key(
    red_tracker, red_upload_paths
) -> None:
    red_tracker.api_key = "red-api-key"
    files = UploadFiles(torrent_data=b"torrent", log_files=[("rip.log", b"log data")])

    result = await red_tracker.upload({"title": "CD rip"}, files)

    assert result == (3, 4)
    assert [path for path, _ in red_upload_paths] == ["site"]


async def test_red_upload_without_logs_with_api_key_uses_api_upload(
    red_tracker, red_upload_paths, upload_files
) -> None:
    red_tracker.api_key = "red-api-key"

    result = await red_tracker.upload({"title": "WEB"}, upload_files)

    assert result == (1, 2)
    assert [path for path, _ in red_upload_paths] == ["api"]


async def test_red_upload_without_logs_without_api_key_uses_site_page_upload(
    red_tracker, red_upload_paths, upload_files
) -> None:
    result = await red_tracker.upload({"title": "WEB"}, upload_files)

    assert result == (3, 4)
    assert [path for path, _ in red_upload_paths] == ["site"]


# ---------------------------------------------------------------------------
# RED: group enrichment via upload.php scraping
# ---------------------------------------------------------------------------

RED_UPLOAD_FORM_HTML = """
<html><body><form>
<div>
  <input name="artists[]" value="Artist A"/>
  <select name="importance[]"><option value="1" selected>Main</option></select>
</div>
<div>
  <input name="artists[]" value="Artist B"/>
  <select name="importance[]"><option value="1">Main</option><option value="2" selected>Guest</option></select>
</div>
<input name="title" value="Some Album"/>
<input name="year" value="2020"/>
<input name="tags" value="electronic, ambient"/>
<input name="image" value="https://img.example/cover.jpg"/>
<select name="releasetype"><option value="1">Album</option><option value="5" selected>EP</option></select>
<textarea name="album_desc">A great record.</textarea>
</form></body></html>
"""


async def test_red_site_page_upload_with_groupid_enriches_from_upload_form(
    red_tracker, red_upload_paths, upload_files, monkeypatch
) -> None:
    requests: list[tuple[str, str]] = []

    async def fake_request(method, url, **kwargs):
        requests.append((method, url))
        return HttpResponse(text=RED_UPLOAD_FORM_HTML, url=url, status=200)

    monkeypatch.setattr(red_tracker, "_request", fake_request)

    result = await red_tracker.site_page_upload({"groupid": "1234", "title": "stale title"}, upload_files)

    assert result == (3, 4)
    assert requests == [("GET", "https://redacted.sh/upload.php?groupid=1234")]
    _, data = red_upload_paths[0]
    assert data["groupid"] == "1234"
    assert data["artists[]"] == ["Artist A", "Artist B"]
    assert data["importance[]"] == [1, 2]
    assert data["title"] == "Some Album"  # scraped value overwrites caller's
    assert data["year"] == "2020"
    assert data["tags"] == "electronic, ambient"
    assert data["image"] == "https://img.example/cover.jpg"
    assert data["releasetype"] == "5"
    assert data["album_desc"] == "A great record."


async def test_red_site_page_upload_without_groupid_skips_enrichment(
    red_tracker, red_upload_paths, upload_files, monkeypatch
) -> None:
    async def fail_request(*args, **kwargs):
        raise AssertionError("no HTTP request expected without groupid")

    monkeypatch.setattr(red_tracker, "_request", fail_request)

    result = await red_tracker.site_page_upload({"title": "New group"}, upload_files)

    assert result == (3, 4)
    assert red_upload_paths == [("site", {"title": "New group"})]


def _parse(data: dict, html: str) -> dict:
    red._parse_upload_form(data, BeautifulSoup(html, "lxml"))
    return data


def test_red_parse_upload_form_defaults_importance_to_main() -> None:
    html = '<form><input name="artists[]" value="Solo"/></form>'

    data = _parse({}, html)

    assert data["artists[]"] == ["Solo"]
    assert data["importance[]"] == [1]


def test_red_parse_upload_form_select_without_selected_option_defaults_to_main() -> None:
    html = (
        '<form><input name="artists[]" value="Solo"/>'
        '<select name="importance[]"><option value="2">Guest</option></select></form>'
    )

    data = _parse({}, html)

    assert data["importance[]"] == [1]


def test_red_parse_upload_form_skips_artists_without_value() -> None:
    html = (
        '<form><input name="artists[]" value=""/>'
        '<input name="artists[]"/>'
        '<input name="artists[]" value="Kept"/></form>'
    )

    data = _parse({}, html)

    assert data["artists[]"] == ["Kept"]
    assert data["importance[]"] == [1]


def test_red_parse_upload_form_empty_page_leaves_data_untouched() -> None:
    data = _parse({"title": "keep me"}, "<html><body></body></html>")

    assert data == {"title": "keep me"}


def test_red_parse_upload_form_ignores_empty_input_values() -> None:
    html = '<form><input name="title" value=""/><input name="year" value="1999"/></form>'

    data = _parse({}, html)

    assert "title" not in data
    assert data["year"] == "1999"


def test_red_parse_upload_form_releasetype_without_selection_not_set() -> None:
    html = '<form><select name="releasetype"><option value="1">Album</option></select></form>'

    data = _parse({}, html)

    assert "releasetype" not in data


# ---------------------------------------------------------------------------
# trackers/__init__: get_class and code maps
# ---------------------------------------------------------------------------


def test_get_class_returns_site_specific_api_classes() -> None:
    assert trackers.get_class("RED") is red.RedApi
    assert trackers.get_class("OPS") is ops.OpsApi
    assert trackers.get_class("DIC") is dic.DICApi


def test_get_class_unknown_site_code_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        trackers.get_class("PTP")


def test_tracker_url_code_map_pins_known_domains() -> None:
    assert trackers.tracker_url_code_map == {
        "redacted.sh": "RED",
        "orpheus.network": "OPS",
        "dicmusic.com": "DIC",
    }


# ---------------------------------------------------------------------------
# trackers/__init__: choose_tracker
# ---------------------------------------------------------------------------


async def test_choose_tracker_accepts_exact_choice(monkeypatch) -> None:
    prompts = patch_tracker_prompt(monkeypatch, ["OPS"])

    assert await trackers.choose_tracker(["RED", "OPS"]) == "OPS"
    assert len(prompts) == 1


async def test_choose_tracker_uppercases_and_strips_input(monkeypatch) -> None:
    patch_tracker_prompt(monkeypatch, ["  red  "])

    assert await trackers.choose_tracker(["RED", "OPS"]) == "RED"


async def test_choose_tracker_accepts_first_letter_shortcut(monkeypatch) -> None:
    patch_tracker_prompt(monkeypatch, ["o"])

    assert await trackers.choose_tracker(["RED", "OPS"]) == "OPS"


async def test_choose_tracker_none_input_returns_none(monkeypatch) -> None:
    patch_tracker_prompt(monkeypatch, ["no thanks"])

    assert await trackers.choose_tracker(["RED", "OPS"]) is None


async def test_choose_tracker_reprompts_until_valid_input(monkeypatch) -> None:
    prompts = patch_tracker_prompt(monkeypatch, ["XYZ", "banana", "dic"])

    assert await trackers.choose_tracker(["RED", "OPS", "DIC"]) == "DIC"
    assert len(prompts) == 3


async def test_choose_tracker_offers_first_choice_as_default(monkeypatch) -> None:
    prompts = patch_tracker_prompt(monkeypatch, ["OPS"])

    await trackers.choose_tracker(["RED", "OPS"])

    assert prompts[0]["default"] == "RED"


# ---------------------------------------------------------------------------
# trackers/__init__: choose_tracker_first_time
# ---------------------------------------------------------------------------


async def test_choose_tracker_first_time_single_tracker_short_circuits(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["DIC"])
    prompts = patch_tracker_prompt(monkeypatch, [])

    assert await trackers.choose_tracker_first_time() == "DIC"
    assert prompts == []


async def test_choose_tracker_first_time_uses_default_tracker(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED", "OPS"])
    monkeypatch.setattr(trackers.tracker_cfg, "default_tracker", "OPS")
    prompts = patch_tracker_prompt(monkeypatch, [])

    assert await trackers.choose_tracker_first_time() == "OPS"
    assert prompts == []


async def test_choose_tracker_first_time_prompts_without_default(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED", "OPS"])
    monkeypatch.setattr(trackers.tracker_cfg, "default_tracker", None)
    prompts = patch_tracker_prompt(monkeypatch, ["ops"])

    assert await trackers.choose_tracker_first_time() == "OPS"
    assert len(prompts) == 1


# ---------------------------------------------------------------------------
# trackers/__init__: validate_tracker (click callback for 'salmon up -t')
# ---------------------------------------------------------------------------


async def test_validate_tracker_accepts_configured_tracker_case_insensitive(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED", "OPS"])
    prompts = patch_tracker_prompt(monkeypatch, [])

    assert await trackers.validate_tracker(None, "tracker", "red") == "RED"
    assert await trackers.validate_tracker(None, "tracker", "OPS") == "OPS"
    assert prompts == []


async def test_validate_tracker_unknown_value_falls_back_to_prompt(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED", "OPS"])
    prompts = patch_tracker_prompt(monkeypatch, ["ops"])

    assert await trackers.validate_tracker(None, "tracker", "PTP") == "OPS"
    assert len(prompts) == 1


async def test_validate_tracker_none_value_uses_first_time_flow(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED"])
    prompts = patch_tracker_prompt(monkeypatch, [])

    assert await trackers.validate_tracker(None, "tracker", None) == "RED"
    assert prompts == []


async def test_validate_tracker_none_value_prefers_default_tracker(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED", "OPS"])
    monkeypatch.setattr(trackers.tracker_cfg, "default_tracker", "RED")
    prompts = patch_tracker_prompt(monkeypatch, [])

    assert await trackers.validate_tracker(None, "tracker", None) == "RED"
    assert prompts == []


async def test_validate_tracker_non_string_value_raises_bad_parameter(monkeypatch) -> None:
    monkeypatch.setattr(trackers, "tracker_list", ["RED", "OPS"])

    with pytest.raises(click.BadParameter, match="This flag requires a tracker"):
        await trackers.validate_tracker(None, "tracker", 42)


# ---------------------------------------------------------------------------
# trackers/__init__: validate_request
# ---------------------------------------------------------------------------


@pytest.fixture
def gazelle_site() -> types.SimpleNamespace:
    return types.SimpleNamespace(base_url="https://redacted.sh")


def test_validate_request_none_passthrough(gazelle_site) -> None:
    assert trackers.validate_request(gazelle_site, None) is None


def test_validate_request_plain_id_returned(gazelle_site) -> None:
    assert trackers.validate_request(gazelle_site, "123") == "123"


def test_validate_request_id_with_whitespace_returned_stripped(gazelle_site) -> None:
    assert trackers.validate_request(gazelle_site, " 123 ") == "123"


def test_validate_request_url_extracts_id(gazelle_site) -> None:
    url = "https://redacted.sh/requests.php?action=view&id=555"

    assert trackers.validate_request(gazelle_site, url) == "555"


def test_validate_request_url_without_id_raises_bad_parameter(gazelle_site) -> None:
    with pytest.raises(click.BadParameter, match="requires a request"):
        trackers.validate_request(gazelle_site, "https://redacted.sh/requests.php?action=view")


def test_validate_request_non_string_raises_bad_parameter(gazelle_site) -> None:
    with pytest.raises(click.BadParameter, match="requires a request"):
        trackers.validate_request(gazelle_site, 555)


def test_validate_request_arbitrary_string_raises_bad_parameter(gazelle_site) -> None:
    with pytest.raises(click.BadParameter, match="requires a request"):
        trackers.validate_request(gazelle_site, "not-a-request")


def test_validate_request_foreign_tracker_url_raises_bad_parameter(gazelle_site) -> None:
    with pytest.raises(click.BadParameter, match="requires a request"):
        trackers.validate_request(gazelle_site, "https://orpheus.network/requests.php?action=view&id=99")
