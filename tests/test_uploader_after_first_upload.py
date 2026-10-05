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
    metadata = {"artists": [("Artist", "main")], "title": "Album", "label": "Label", "cover": None}
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
        "red_blacklist_reason": _sync(None),
        "collect_upload_warnings": _sync([]),
        "check_requests": recording("check_requests"),
        "check_existing_group": recording("check_existing_group", 5),
        "upload_and_report": upload_and_report,
        "print_torrents": recording("print_torrents"),
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


def test_a_failed_group_fetch_after_the_upload_still_runs_the_spectral_check(flow, monkeypatch) -> None:
    calls, executed, set_fake = flow
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)

    async def fetch_fails(*_args, **_kwargs):
        raise click.Abort

    set_fake("print_torrents", fetch_fails)
    _upload(None)
    names = [name for name, _site, _kw in calls]
    assert names.count("post_upload_spectral_check") == 1
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
