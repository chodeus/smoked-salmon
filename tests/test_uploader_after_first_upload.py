"""After the first upload: spectrals checked once, no offer to delete the uploaded folder, a clean abort."""

import contextlib
import os
from typing import Any

import anyio
import asyncclick as click
import pytest

import salmon.trackers
import salmon.uploader
from salmon.errors import AbortAndDeleteFolder, RequestError, UnknownOutcomeError
from salmon.uploader import dupe_checker, spectrals


class FakeSite:
    base_url = "https://tracker.test"

    def __init__(self, code: str = "RED") -> None:
        self.site_code = code
        self.site_string = code
        self.reports: list[tuple[int, str, str]] = []
        self.edit_error: Exception | None = None
        self.report_error: Exception | None = None

    async def append_to_torrent_description(self, torrent_id: int, text: str) -> None:
        if self.edit_error is not None:
            raise self.edit_error

    async def report_lossy_master(self, torrent_id: int, comment: str, source: str) -> bool:
        if self.report_error is not None:
            raise self.report_error
        self.reports.append((torrent_id, comment, source))
        return True


@contextlib.contextmanager
def _staged_as_is(path: str, scratch: bool):
    yield path, None


def _sync(result: Any = None):
    def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _answers(monkeypatch, *answers: str) -> list[str]:
    """Answer click.prompt with answers in turn. Returns the prompts asked."""
    asked: list[str] = []

    async def fake_prompt(text: str, *_args, **_kwargs) -> str:
        asked.append(click.unstyle(text))
        return answers[len(asked) - 1]

    monkeypatch.setattr(click, "prompt", fake_prompt)
    return asked


@pytest.fixture
def flow(monkeypatch):
    """Stub upload()'s seams. Returns (calls, executed, set_fake); calls holds (name, site code, kwargs)."""
    calls: list[tuple[str, str | None, dict]] = []
    executed: list[bool] = []
    overrides: dict[str, Any] = {}

    def recording(name: str, result: Any = None):
        async def fake(*args, **kwargs) -> Any:
            site = args[0] if args else None
            calls.append((name, getattr(site, "site_code", None), kwargs))
            if name in overrides:
                return await overrides[name](*args, **kwargs)
            return result

        return fake

    uploads = iter(range(1, 100))

    async def upload_and_report(site, *_args, **_kwargs):
        torrent_id = next(uploads)
        calls.append(("upload_and_report", site.site_code, {}))
        return torrent_id, 5, "/t.torrent", b"", f"{site.base_url}/torrents.php?torrentid={torrent_id}"

    class FakeUploadManager:
        async def execute_upload(self) -> None:
            executed.append(True)

    rls_data = {
        "artists": [("Artist", "main")],
        "title": "Album",
        "catno": "",
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "year": 2020,
    }
    metadata = {"artists": [("Artist", "main")], "title": "Album", "label": "Label", "catno": None, "cover": None}
    for name, fake in {
        "release_type_from_folder": _sync(None),
        "conversion_of": _sync(None),
        "staged_source": _staged_as_is,
        "gather_audio_info": _sync({}),
        "check_hybrid": _sync(False),
        "standardize_tags": _sync(),
        "gather_tags": _sync({}),
        "construct_rls_data": _sync(rls_data),
        "mqa_test": recording("mqa_test"),
        "get_metadata": recording("get_metadata", (metadata, None)),
        "edit_metadata": recording("edit_metadata", ("/release", metadata, {}, {})),
        "concat_track_data": _sync({"01.flac": {}}),
        "resolve_cover_url": recording("resolve_cover_url", (True, None)),
        "strip_oversized_pictures": _sync(False),
        "collect_upload_warnings": _sync([]),
        "check_requests": recording("check_requests"),
        "check_existing_group": recording("check_existing_group", 5),
        "recheck_edition": recording("recheck_edition", 5),
        "upload_and_report": upload_and_report,
        "print_torrents": recording("print_torrents", {}),
        "post_upload_spectral_check": recording("post_upload_spectral_check", (False, None, None, None)),
        "get_downconversion_options": _sync([]),
        "UploadManager": FakeUploadManager,
    }.items():
        monkeypatch.setattr(salmon.uploader, name, fake)
    monkeypatch.setattr(salmon.trackers, "get_class", lambda code: lambda: FakeSite(code))
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", False)
    monkeypatch.setattr(salmon.uploader.cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(salmon.uploader.cfg.image, "auto_compress_cover", False)

    def set_fake(name: str, fake) -> None:
        overrides[name] = fake

    return calls, executed, set_fake


def _upload(trackers: list[str] | None) -> None:
    anyio.run(
        lambda: salmon.uploader.upload(
            FakeSite("RED"),  # type: ignore[arg-type]
            "/release",
            5,
            "WEB",
            None,
            (),
            None,
            spectrals_after=True,
            trackers=trackers,
        )
    )


def test_spectrals_after_runs_once_the_only_upload_is_up(flow, monkeypatch) -> None:
    calls, executed, _ = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    _upload(None)
    names = [name for name, _site, _kw in calls]
    assert names.count("post_upload_spectral_check") == 1
    assert names.index("upload_and_report") < names.index("post_upload_spectral_check")
    assert executed == [True]


def test_an_upload_with_no_torrent_id_skips_the_spectral_check(flow, monkeypatch, capsys) -> None:
    calls, executed, _ = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)

    async def request_fill(site, *_args, **_kwargs):
        return 0, 5, "/t.torrent", b"", f"{site.base_url}/requests.php?action=view&id=7"

    monkeypatch.setattr(salmon.uploader, "upload_and_report", request_fill)
    _upload(None)
    names = [name for name, _site, _kw in calls]
    out = capsys.readouterr().out
    assert "post_upload_spectral_check" not in names
    assert "salmon checkspecs" in out
    assert executed == [True]


def test_the_release_type_hint_reads_the_chosen_title_not_the_tags(flow, monkeypatch) -> None:
    calls, _executed, set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(
        salmon.uploader, "gather_audio_info", lambda *_args, **_kwargs: {str(n): {"duration": 200} for n in range(7)}
    )
    chosen = {"artists": [("Artist", "main")], "title": "Album EP", "label": "Label", "cover": None}

    async def get_metadata(*_args, **_kwargs):
        return chosen, None

    set_fake("get_metadata", get_metadata)
    _upload(None)
    hints = [kwargs["rls_type_hint"] for name, _site, kwargs in calls if name == "edit_metadata"]

    assert hints == [None]


def test_a_failed_group_fetch_after_the_upload_still_runs_the_spectral_check(flow, monkeypatch) -> None:
    calls, executed, set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)

    async def fetch_fails(*_args, **_kwargs):
        raise click.Abort

    set_fake("print_torrents", fetch_fails)
    _upload(None)
    names = [name for name, _site, _kw in calls]
    assert names.count("post_upload_spectral_check") == 1
    assert names.index("post_upload_spectral_check") < names.index("print_torrents")
    assert executed == [True]


def test_the_next_tracker_never_offers_to_delete_the_uploaded_folder(flow, monkeypatch) -> None:
    calls, _executed, _ = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", True)
    _upload(["RED", "OPS"])
    names = [(name, site) for name, site, _kw in calls]
    assert names.count(("post_upload_spectral_check", "RED")) == 1
    assert names.index(("post_upload_spectral_check", "RED")) < names.index(("upload_and_report", "OPS"))
    ops_search = [kw for name, site, kw in calls if name == "check_existing_group" and site == "OPS"]
    assert ops_search == [{"offer_deletion": False, "release": ops_search[0]["release"]}]


def test_an_abort_after_an_upload_lists_it_and_still_seeds(flow, monkeypatch, capsys) -> None:
    calls, executed, set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", True)

    async def abort(*_args, **_kwargs):
        raise click.Abort

    set_fake("check_existing_group", abort)
    _upload(["RED", "OPS"])
    out = capsys.readouterr().out
    assert "Already uploaded:" in out
    assert "https://tracker.test/torrents.php?torrentid=1" in out
    assert executed == [True]
    assert [site for name, site, _kw in calls if name == "upload_and_report"] == ["RED"]


def test_an_abort_before_any_upload_still_aborts(flow, monkeypatch) -> None:
    _calls, executed, set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)

    async def abort(*_args, **_kwargs):
        raise click.Abort

    set_fake("resolve_cover_url", abort)
    with pytest.raises(click.Abort):
        _upload(None)
    assert executed == [True]


def _no_torrents(monkeypatch) -> None:
    async def print_torrents(_site, group_id, rset=None, **_kwargs):
        return {"groupId": group_id, "torrents": []}

    monkeypatch.setattr(dupe_checker, "print_torrents", print_torrents)


def test_the_group_confirmation_hides_delete_once_a_torrent_is_up(monkeypatch) -> None:
    _no_torrents(monkeypatch)
    asked = _answers(monkeypatch, "d", "y")
    confirmed = anyio.run(lambda: dupe_checker._confirm_group_id(FakeSite(), 5, [], None, offer_deletion=False))  # type: ignore[arg-type]
    assert confirmed is True
    assert len(asked) == 2
    assert all("[d]elete" not in text for text in asked)


def test_the_group_confirmation_still_offers_delete_before_any_upload(monkeypatch) -> None:
    _no_torrents(monkeypatch)
    asked = _answers(monkeypatch, "d")
    with pytest.raises(AbortAndDeleteFolder):
        anyio.run(lambda: dupe_checker._confirm_group_id(FakeSite(), 5, [], None))  # type: ignore[arg-type]
    assert "[d]elete music folder" in asked[0]


def test_the_lossy_master_prompt_hides_delete_when_asked(monkeypatch) -> None:
    asked = _answers(monkeypatch, "d", "n")
    answer = anyio.run(lambda: spectrals.prompt_lossy_master(True, offer_deletion=False))
    assert answer is False
    assert all("[d]elete" not in text for text in asked)


def test_the_post_upload_check_never_offers_to_delete(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def check_spectrals(*_args, **kwargs):
        seen.update(kwargs)
        return False, None

    monkeypatch.setattr(spectrals, "check_spectrals", check_spectrals)
    anyio.run(lambda: spectrals.post_upload_spectral_check(FakeSite(), "/release", 1, None, {}, "WEB", None))  # type: ignore[arg-type]
    assert seen["offer_deletion"] is False


def _lossy_post_upload_check(monkeypatch, site: FakeSite) -> None:
    async def check_spectrals(*_args, **_kwargs):
        return True, {1: "01.flac"}

    monkeypatch.setattr(spectrals, "check_spectrals", check_spectrals)
    monkeypatch.setattr(spectrals, "generate_lossy_approval_comment", lambda *_a, **_k: _done("lossy note"))
    monkeypatch.setattr(spectrals, "handle_spectrals_upload_and_deletion", lambda *_a: _done({1: ["full", "zoom"]}))
    anyio.run(lambda: spectrals.post_upload_spectral_check(site, "/release", 7, None, {}, "WEB", None))  # type: ignore[arg-type]


async def _done(value: Any) -> Any:
    return value


@pytest.mark.parametrize(
    ("error", "wording"),
    [
        (RequestError("edit refused"), "was not updated on RED (edit refused). Paste this in by hand"),
        (UnknownOutcomeError("answer lost"), "Could not tell whether RED took the description edit"),
    ],
)
def test_a_failed_description_edit_keeps_the_bbcode_and_the_report(monkeypatch, capsys, error, wording) -> None:
    site = FakeSite()
    site.edit_error = error
    _lossy_post_upload_check(monkeypatch, site)
    out = capsys.readouterr().out
    assert wording in out
    assert "[hide=Spectrals]" in out
    assert [report[0] for report in site.reports] == [7]


def test_a_refused_lossy_master_report_is_printed_to_file_by_hand(monkeypatch, capsys) -> None:
    site = FakeSite()
    site.report_error = RequestError("already reported")
    _lossy_post_upload_check(monkeypatch, site)
    out = capsys.readouterr().out
    assert "did not take the lossy master report" in out
    assert "lossy note" in out


def _nothing_picked(monkeypatch, tmp_path) -> str:
    async def check_spectrals(*_args, **_kwargs):
        return False, None

    monkeypatch.setattr(spectrals, "check_spectrals", check_spectrals)
    monkeypatch.setattr(spectrals.cfg.directory, "tmp_dir", None)
    album = tmp_path / "Album"
    album.mkdir()
    return str(album)


def test_spectrals_nobody_picked_are_removed_before_a_later_torrent(monkeypatch, tmp_path) -> None:
    album = _nothing_picked(monkeypatch, tmp_path)
    made = spectrals.create_specs_folder(album)
    anyio.run(lambda: spectrals.post_upload_spectral_check(FakeSite(), album, 1, None, {}, "WEB", None))  # type: ignore[arg-type]
    assert not os.path.exists(made)


def test_a_spectrals_folder_salmon_did_not_make_is_kept_when_nothing_is_picked(monkeypatch, tmp_path) -> None:
    album = _nothing_picked(monkeypatch, tmp_path)
    theirs = os.path.join(album, "Spectrals")
    os.mkdir(theirs)
    anyio.run(lambda: spectrals.post_upload_spectral_check(FakeSite(), album, 1, None, {}, "WEB", None))  # type: ignore[arg-type]
    assert os.path.isdir(theirs)


def test_every_trackers_conversions_are_checked_against_the_runs_path_limit(flow, monkeypatch) -> None:
    _calls, _executed, _set_fake = flow
    task = {"name": "MP3 320", "action": "transcode", "encoding": "320"}
    limits: list[int | None] = []

    async def choose(*_args):
        return [task]

    async def execute(*_args, max_path_length=None, **_kwargs):
        limits.append(max_path_length)

    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    monkeypatch.setattr(salmon.uploader, "get_downconversion_options", lambda *_args: [task])
    monkeypatch.setattr(salmon.uploader, "prompt_downconversion_choice", choose)
    monkeypatch.setattr(salmon.uploader, "execute_downconversion_tasks", execute)
    _upload(["RED", "OPS"])

    assert limits == [180, 180]


def test_an_albums_own_spectrals_folder_survives_the_real_check(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(spectrals.cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(spectrals.cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "Album"
    (album / "Spectrals").mkdir(parents=True)
    (album / "Spectrals" / "mine.png").write_bytes(b"mine")
    made: list[str] = []

    async def generate(_path, spectrals_path, _audio_info):
        made.append(spectrals_path)
        return {}

    monkeypatch.setattr(spectrals, "generate_spectrals_all", generate)
    monkeypatch.setattr(spectrals, "print_frequency_guidance", lambda *_a: _done("ok"))
    monkeypatch.setattr(spectrals, "view_spectrals", lambda *_a: _done(None))
    monkeypatch.setattr(spectrals, "prompt_lossy_master", lambda *_a, **_k: _done(False))
    monkeypatch.setattr(spectrals, "prompt_spectrals", lambda *_a, **_k: _done({}))
    anyio.run(lambda: spectrals.post_upload_spectral_check(FakeSite(), str(album), 1, None, {}, "WEB", None))  # type: ignore[arg-type]

    assert (album / "Spectrals" / "mine.png").read_bytes() == b"mine"
    assert os.path.dirname(made[0]) == str(tmp_path)
    assert not os.path.exists(made[0])


def test_an_upload_without_spectrals_leaves_the_albums_own_spectrals_folder(monkeypatch, tmp_path) -> None:
    album = _nothing_picked(monkeypatch, tmp_path)
    own = os.path.join(album, "Spectrals")
    os.mkdir(own)

    anyio.run(lambda: spectrals.handle_spectrals_upload_and_deletion(spectrals.get_spectrals_path(album), None))

    assert os.path.isdir(own)


def test_an_upload_with_no_group_id_offers_no_conversions(flow, monkeypatch, capsys) -> None:
    calls, executed, _ = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    offered: list[bool] = []

    async def request_fill(site, *_args, **_kwargs):
        return 7, 0, "/t.torrent", b"", f"{site.base_url}/torrents.php?torrentid=7"

    async def choose(*_args):
        offered.append(True)
        return []

    monkeypatch.setattr(salmon.uploader, "upload_and_report", request_fill)
    monkeypatch.setattr(salmon.uploader, "get_downconversion_options", lambda *_args: [{"name": "MP3 320"}])
    monkeypatch.setattr(salmon.uploader, "prompt_downconversion_choice", choose)
    _upload(None)

    assert offered == []
    assert "print_torrents" not in [name for name, _site, _kw in calls]
    out = capsys.readouterr().out
    assert "No group id came back" in out
    assert executed == [True]


def _upload_unpicked(trackers: list[str] | None, request_id: int | None = None) -> None:
    """An upload with no group given: the first tracker is searched before the review."""
    anyio.run(
        lambda: salmon.uploader.upload(
            FakeSite("RED"),  # type: ignore[arg-type]
            "/release",
            None,
            "WEB",
            None,
            (),
            None,
            request_id=request_id,
            spectrals_after=True,
            trackers=trackers,
        )
    )


def _red_search_fails(set_fake) -> None:
    async def search(site, *_args, **_kwargs):
        if site.site_code == "RED":
            raise RequestError("RED is down")
        return 5

    set_fake("check_existing_group", search)


def test_a_first_tracker_whose_search_fails_is_skipped_for_the_next(flow, capsys) -> None:
    calls, executed, set_fake = flow
    _red_search_fails(set_fake)

    _upload_unpicked(["RED", "OPS"])

    assert [site for name, site, _kw in calls if name == "check_existing_group"] == ["RED", "OPS"]
    assert [site for name, site, _kw in calls if name == "upload_and_report"] == ["OPS"]
    out = capsys.readouterr().out
    assert "Could not search RED for dupes (RED is down): skipping it." in out
    assert executed == [True]


@pytest.mark.parametrize(
    ("trackers", "request_id"), [(None, None), (["RED", "OPS"], 7)], ids=["none-to-follow", "request"]
)
def test_a_first_tracker_whose_search_fails_ends_the_run_when_none_can_follow(
    flow, monkeypatch, capsys, trackers, request_id
) -> None:
    calls, _executed, set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    _red_search_fails(set_fake)

    _upload_unpicked(trackers, request_id)

    assert "upload_and_report" not in [name for name, _site, _kw in calls]
    out = capsys.readouterr().out
    assert "Could not search RED for dupes: RED is down" in out
    assert "Aborting upload" in out


@pytest.mark.parametrize("given", [True, False], ids=["given", "picked"])
def test_the_first_trackers_group_is_weighed_against_the_reviewed_edition(flow, monkeypatch, given: bool) -> None:
    _calls, _executed, set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    seen: list[tuple] = []

    async def recheck(_site, group_id, release, weighed_against):
        seen.append((group_id, release["title"], weighed_against))
        return group_id

    async def edit_metadata(*_args, **_kwargs):
        reviewed = {"artists": [("Artist", "main")], "title": "Album (Reviewed)", "label": "Label", "catno": None}
        return "/release", {**reviewed, "cover": None}, {}, {}

    set_fake("recheck_edition", recheck)
    set_fake("edit_metadata", edit_metadata)
    if given:
        _upload(None)
    else:
        _upload_unpicked(None)

    assert [(group_id, title) for group_id, title, _weighed in seen] == [(5, "Album (Reviewed)")]
    assert (seen[0][2] is None) is given


def test_a_pick_is_weighed_as_the_tags_had_it_though_the_scrape_edits_them(flow, monkeypatch) -> None:
    _calls, _executed, set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    weighed_years: list[int] = []

    async def get_metadata(_path, _tags, rls_data):
        # As combine_metadatas does with its base: the chosen scrape lands in the tags' release itself.
        rls_data["year"] = 2005
        return rls_data, None

    async def recheck(_site, group_id, _release, weighed_against):
        weighed_years.append(weighed_against["year"])
        return group_id

    set_fake("get_metadata", get_metadata)
    set_fake("recheck_edition", recheck)
    _upload_unpicked(None)

    assert weighed_years == [2020]


def test_conversions_the_edition_already_holds_are_left_out_after_the_flac(flow, monkeypatch, capsys) -> None:
    _calls, _executed, set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    reviewed = {
        "artists": [("Artist", "main")],
        "title": "Album",
        "label": "Label",
        "catno": None,
        "cover": None,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "year": 2020,
    }
    group = {"group": {"year": 2020}, "torrents": [{"id": 9, "media": "WEB", "format": "MP3", "encoding": "V0 (VBR)"}]}
    offered: list[set[str]] = []

    async def edit_metadata(*_args, **_kwargs):
        return "/release", reviewed, {}, {}

    async def printed(*_args, **_kwargs):
        return group

    async def choose(_rls_data, _track_data, held):
        offered.append(held)
        return []

    options = [
        {"name": "MP3 V0", "action": "transcode", "encoding": "V0"},
        {"name": "MP3 320", "action": "transcode", "encoding": "320"},
    ]
    set_fake("edit_metadata", edit_metadata)
    set_fake("print_torrents", printed)
    monkeypatch.setattr(salmon.uploader, "get_downconversion_options", lambda *_args: options)
    monkeypatch.setattr(salmon.uploader, "prompt_downconversion_choice", choose)
    _upload(None)

    assert offered == [{"MP3 V0"}]
    out = capsys.readouterr().out
    assert "DUPE RISK: this edition already has MP3 V0" in out


def test_a_listed_first_tracker_is_not_weighed_or_searched(flow, monkeypatch) -> None:
    calls, _executed, _set_fake = flow
    monkeypatch.setattr(
        salmon.uploader, "do_not_upload_reason", lambda tracker, _release: "listed" if tracker == "RED" else None
    )

    _upload(["RED", "OPS"])

    names_and_sites = [(name, site) for name, site, _kw in calls]
    assert ("recheck_edition", "RED") not in names_and_sites
    assert [site for name, site in names_and_sites if name == "upload_and_report"] == ["OPS"]


@pytest.mark.parametrize("conversion", [None, {"source": "Album [24-96]"}], ids=["an-album", "salmons-conversion"])
def test_the_markers_are_weighed_except_on_salmons_own_conversions(flow, monkeypatch, conversion) -> None:
    _calls, _executed, _set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(salmon.uploader, "conversion_of", lambda _path: conversion)
    monkeypatch.setattr(salmon.uploader, "converted_from_note", lambda *_args: None)
    warned: list[str] = []
    monkeypatch.setattr(salmon.uploader, "_warn_about_provenance", warned.append)

    _upload(None)

    assert warned == ([] if conversion else ["/release"])
