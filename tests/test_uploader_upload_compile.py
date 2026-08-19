"""Tests for the upload compilation pipeline.

Covers src/salmon/uploader/upload.py (form-data compilation, file collection,
torrent generation, prepare_and_upload) plus upload_and_report and
_prompt_source from src/salmon/uploader/__init__.py.
"""

import importlib
import os
from types import SimpleNamespace

import asyncclick as click
import pytest
from torf import Torrent

from salmon import cfg
from salmon.constants import RELEASE_TYPES
from salmon.errors import RequestError, UploadError
from salmon.uploader import _prompt_source, convert_genres, upload_and_report
from salmon.uploader.upload import (
    collect_logfiles,
    compile_data_existing_group,
    compile_data_new_group,
    compile_files,
    concat_track_data,
    generate_catno,
    generate_description,
    generate_t_description,
    generate_torrent,
    prepare_and_upload,
)

FOOTER = (
    "[hr]Uploaded with [url=https://github.com/smokin-salmon/smoked-salmon]"
    "[b]smoked-salmon[/b] v1.0.0-test[/url]"
)

EXPECTED_ALBUM_DESC = "[b][size=4]Tracklist[/size][/b]\n[b]01.[/b] Testartist - Intro [i](3:05)[/i]\n"

EXPECTED_RELEASE_DESC = (
    "[img]https://ptpimg.me/67vp4c.png[/img] [b]16 bit [color=#2E86C1]44.1[/color] kHz[/b]\n"
    "Released on [b]2024-03-01[/b]\n" + FOOTER
)

COVER_URL = "https://example.com/cover.jpg"


def make_metadata(**overrides):
    """A representative, fully-populated metadata dict for a WEB FLAC release."""
    md = {
        "title": "Testalbum",
        "artists": [("Testartist", "main")],
        "group_year": 2023,
        "year": 2024,
        "edition_title": "Deluxe Edition",
        "label": "Test Label",
        "catno": "TL-001",
        "upc": "123456789012",
        "rls_type": "Album",
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "source": "WEB",
        "scene": False,
        "tags": "electronic",
        "comment": None,
        "urls": [],
        "date": "2024-03-01",
    }
    md.update(overrides)
    return md


def make_track(tracknumber="1", discnumber="1", title="Intro", duration=185, precision=16):
    return {
        "duration": duration,
        "sample rate": 44100,
        "bit rate": 941000,
        "precision": precision,
        "t": SimpleNamespace(
            discnumber=discnumber,
            tracknumber=tracknumber,
            artist=["Testartist"],
            title=title,
        ),
    }


def make_track_data():
    return {"01. Intro.flac": make_track()}


@pytest.fixture
def pinned_cfg(monkeypatch):
    """Deterministic config + version for exact description/form pinning."""
    monkeypatch.setattr(cfg.upload.description, "icons_in_descriptions", True)
    monkeypatch.setattr(cfg.upload.description, "include_tracklist_in_t_desc", False)
    monkeypatch.setattr(cfg.upload.description, "bitrates_in_t_desc", False)
    monkeypatch.setattr(cfg.upload.compression, "lma_comment_in_t_desc", False)
    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", True)
    # NB: "salmon.uploader.upload" as a dotted monkeypatch target resolves to the
    # upload() *function* re-exported by the package, so fetch the real module.
    upload_module = importlib.import_module("salmon.uploader.upload")
    monkeypatch.setattr(upload_module, "get_version", lambda: "1.0.0-test")


@pytest.fixture
def tracker(fake_tracker, tmp_path):
    """fake_tracker completed with the attrs prepare_and_upload needs."""
    fake_tracker.release_types = RELEASE_TYPES
    fake_tracker.dot_torrents_dir = str(tmp_path / "dottorrents")
    os.makedirs(fake_tracker.dot_torrents_dir, exist_ok=True)
    fake_tracker.auth_calls = 0

    async def ensure_authenticated():
        fake_tracker.auth_calls += 1

    fake_tracker.ensure_authenticated = ensure_authenticated
    return fake_tracker


# ---------------------------------------------------------------------------
# compile_data_new_group / compile_data_existing_group
# ---------------------------------------------------------------------------


def test_compile_data_new_group_pins_exact_form_dict(tracker, pinned_cfg):
    data = compile_data_new_group(
        tracker,
        "/fake/path",
        make_metadata(),
        make_track_data(),
        hybrid=False,
        cover_url=COVER_URL,
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=None,
        request_id=None,
    )
    assert data == {
        "submit": True,
        "type": 0,
        "title": "Testalbum",
        "artists[]": ["Testartist"],
        "importance[]": [1],
        "year": 2023,
        "record_label": "Test Label",
        "catalogue_number": "TL-001",
        "releasetype": 1,
        "remaster": True,
        "remaster_year": 2024,
        "remaster_title": "Deluxe Edition",
        "remaster_record_label": "Test Label",
        "remaster_catalogue_number": "TL-001",
        "format": "FLAC",
        "bitrate": "Lossless",
        "other_bitrate": None,
        "vbr": False,
        "media": "WEB",
        "tags": "electronic",
        "image": COVER_URL,
        "album_desc": EXPECTED_ALBUM_DESC,
        "release_desc": EXPECTED_RELEASE_DESC,
        "requestid": None,
    }
    # scene=False must not emit a scene key at all.
    assert "scene" not in data


def test_compile_data_existing_group_pins_exact_form_dict(tracker, pinned_cfg):
    data = compile_data_existing_group(
        tracker,
        "/fake/path",
        4242,
        make_metadata(),
        make_track_data(),
        hybrid=False,
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=None,
        request_id=None,
    )
    assert data == {
        "submit": True,
        "type": 0,
        "groupid": 4242,
        "remaster": True,
        "remaster_year": 2024,
        "remaster_title": "Deluxe Edition",
        "remaster_record_label": "Test Label",
        "remaster_catalogue_number": "TL-001",
        "format": "FLAC",
        "bitrate": "Lossless",
        "other_bitrate": None,
        "vbr": False,
        "media": "WEB",
        "release_desc": EXPECTED_RELEASE_DESC,
        "requestid": None,
    }
    # No group-level fields on an existing-group upload.
    for absent in ("title", "artists[]", "importance[]", "album_desc", "image", "tags"):
        assert absent not in data


def test_compile_data_new_group_maps_artist_roles_to_importances(tracker, pinned_cfg):
    metadata = make_metadata(
        artists=[
            ("Main Guy", "main"),
            ("Feature Gal", "guest"),
            ("Remix Kid", "remixer"),
            ("Composer Person", "composer"),
            ("DJ Comp", "djcompiler"),
        ]
    )
    data = compile_data_new_group(
        tracker, "/p", metadata, make_track_data(), False, COVER_URL, None, None, None, None
    )
    assert data["artists[]"] == ["Main Guy", "Feature Gal", "Remix Kid", "Composer Person", "DJ Comp"]
    assert data["importance[]"] == [1, 2, 3, 4, 6]


def test_compile_data_new_group_unknown_artist_role_raises_keyerror(tracker, pinned_cfg):
    metadata = make_metadata(artists=[("Someone", "bogusrole")])
    with pytest.raises(KeyError):
        compile_data_new_group(tracker, "/p", metadata, make_track_data(), False, COVER_URL, None, None, None, None)


@pytest.mark.parametrize(
    ("rls_type", "expected"),
    [("Album", 1), ("EP", 5), ("Compilation", 7), ("Single", 9), ("Unknown", 21)],
)
def test_compile_data_new_group_maps_release_types(tracker, pinned_cfg, rls_type, expected):
    data = compile_data_new_group(
        tracker, "/p", make_metadata(rls_type=rls_type), make_track_data(), False, COVER_URL, None, None, None, None
    )
    assert data["releasetype"] == expected


def test_compile_data_new_group_unmapped_release_type_raises_keyerror(tracker, pinned_cfg):
    with pytest.raises(KeyError):
        compile_data_new_group(
            tracker, "/p", make_metadata(rls_type="Nonsense"), make_track_data(), False, COVER_URL, None, None, None,
            None,
        )


def test_compile_data_scene_true_adds_scene_key_in_both_forms(tracker, pinned_cfg):
    metadata = make_metadata(scene=True)
    new = compile_data_new_group(tracker, "/p", metadata, make_track_data(), False, COVER_URL, None, None, None, None)
    existing = compile_data_existing_group(
        tracker, "/p", 1, metadata, make_track_data(), False, None, None, None, None
    )
    assert new["scene"] is True
    assert existing["scene"] is True


def test_compile_data_existing_group_scene_false_omits_scene_key(tracker, pinned_cfg):
    data = compile_data_existing_group(
        tracker, "/p", 1, make_metadata(scene=False), make_track_data(), False, None, None, None, None
    )
    assert "scene" not in data


def test_compile_data_request_id_is_passed_through(tracker, pinned_cfg):
    new = compile_data_new_group(
        tracker, "/p", make_metadata(), make_track_data(), False, COVER_URL, None, None, None, 118822
    )
    existing = compile_data_existing_group(
        tracker, "/p", 1, make_metadata(), make_track_data(), False, None, None, None, "990"
    )
    assert new["requestid"] == 118822
    assert existing["requestid"] == "990"


def test_compile_data_hybrid_forces_tracklist_and_drops_encode_specifics(tracker, pinned_cfg):
    data = compile_data_new_group(
        tracker, "/p", make_metadata(), make_track_data(), True, COVER_URL, None, None, None, None
    )
    assert "01. Intro [i](3:05)[/i] [16 bit / 44.1 kHz]" in data["release_desc"]
    assert "Encode Specifics" not in data["release_desc"]
    assert "https://ptpimg.me/67vp4c.png" not in data["release_desc"]


def test_compile_data_new_group_year_and_group_year_go_to_different_fields(tracker, pinned_cfg):
    data = compile_data_new_group(
        tracker, "/p", make_metadata(group_year=1999, year=2010), make_track_data(), False, COVER_URL, None, None,
        None, None,
    )
    assert data["year"] == 1999
    assert data["remaster_year"] == 2010


def test_compile_data_new_group_missing_label_and_empty_tags_pass_through(tracker, pinned_cfg):
    data = compile_data_new_group(
        tracker, "/p", make_metadata(label=None, tags="", catno=None, upc=None), make_track_data(), False, COVER_URL,
        None, None, None, None,
    )
    assert data["record_label"] is None
    assert data["remaster_record_label"] is None
    assert data["tags"] == ""
    assert data["catalogue_number"] == ""


def test_generate_catno_prefers_catno_over_upc(pinned_cfg):
    assert generate_catno(make_metadata()) == "TL-001"


def test_generate_catno_falls_back_to_upc_when_enabled(monkeypatch):
    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", True)
    assert generate_catno(make_metadata(catno=None)) == "123456789012"


def test_generate_catno_empty_when_upc_fallback_disabled(monkeypatch):
    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", False)
    assert generate_catno(make_metadata(catno=None)) == ""


def test_generate_catno_empty_when_no_catno_and_no_upc(monkeypatch):
    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", True)
    metadata = make_metadata(catno=None)
    del metadata["upc"]
    assert generate_catno(metadata) == ""


def test_generate_catno_explicit_none_upc_yields_empty_string(monkeypatch):
    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", True)
    assert generate_catno(make_metadata(catno=None, upc=None)) == ""


def test_convert_genres_normalizes_separators_to_dots():
    assert convert_genres(["Hip Hop", "Drum_and_Bass", "Trip-Hop"]) == "Hip.Hop,Drum.and.Bass,Trip.Hop"
    assert convert_genres([]) == ""


def test_concat_track_data_merges_tags_into_audio_info():
    tags = {"a.flac": "tag-a", "b.flac": "tag-b"}
    audio_info = {"a.flac": {"duration": 1}, "b.flac": {"duration": 2}}
    combined = concat_track_data(tags, audio_info)
    assert combined == {
        "a.flac": {"duration": 1, "t": "tag-a"},
        "b.flac": {"duration": 2, "t": "tag-b"},
    }


# ---------------------------------------------------------------------------
# generate_description (group description / tracklist)
# ---------------------------------------------------------------------------


def test_generate_description_multiple_tracks_totals_length(pinned_cfg):
    track_data = {
        "01. Intro.flac": make_track("1", title="Intro", duration=185),
        "02. Outro.flac": make_track("2", title="Outro", duration=200),
    }
    desc = generate_description(track_data, make_metadata())
    assert "[b]01.[/b] Testartist - Intro [i](3:05)[/i]" in desc
    assert "[b]02.[/b] Testartist - Outro [i](3:20)[/i]" in desc
    assert "[b]Total length: [/b]6:25" in desc


def test_generate_description_single_track_has_no_total_length(pinned_cfg):
    desc = generate_description(make_track_data(), make_metadata())
    assert "Total length" not in desc


def test_generate_description_multi_disc_prefixes_disc_numbers(pinned_cfg):
    track_data = {
        "01. Intro.flac": make_track("1", discnumber="1", title="Intro"),
        "02. Encore.flac": make_track("1", discnumber="2", title="Encore", duration=200),
    }
    desc = generate_description(track_data, make_metadata())
    assert "[b]01-01.[/b] Testartist - Intro" in desc
    assert "[b]02-01.[/b] Testartist - Encore" in desc


def test_generate_description_disc_1_of_1_is_not_multi_disc(pinned_cfg):
    track_data = {"01. Intro.flac": make_track("1", discnumber="1/1")}
    desc = generate_description(track_data, make_metadata())
    assert "[b]01.[/b] Testartist - Intro" in desc
    assert "01-01" not in desc


def test_generate_description_includes_comment_and_more_info(monkeypatch, pinned_cfg):
    monkeypatch.setattr(cfg.upload.description, "icons_in_descriptions", False)
    metadata = make_metadata(comment="Great album.", urls=["https://www.deezer.com/album/12345"])
    desc = generate_description(make_track_data(), metadata)
    assert "\nGreat album.\n" in desc
    assert "[b]More info:[/b] [url=https://www.deezer.com/album/12345]Deezer[/url]" in desc


# ---------------------------------------------------------------------------
# generate_t_description (torrent description)
# ---------------------------------------------------------------------------


def t_desc(metadata=None, track_data=None, hybrid=False, urls=None, spectral_urls=None,
           spectral_ids=None, lossy_comment=None, source_url=None):
    return generate_t_description(
        metadata or make_metadata(),
        track_data or make_track_data(),
        hybrid,
        urls or [],
        spectral_urls,
        spectral_ids,
        lossy_comment,
        source_url,
    )


def test_t_description_includes_spectral_bbcode_when_urls_given(pinned_cfg):
    desc = t_desc(spectral_urls={1: ["u1", "u2"]}, spectral_ids={1: "01. Intro"})
    assert desc.startswith("[hide=Spectrals]")
    assert "[b]01. Intro Full[/b]\n[img=u1]\n[hide=Zoomed][img=u2][/hide]" in desc


def test_t_description_24bit_uses_other_icon(pinned_cfg):
    desc = t_desc(track_data={"01. Intro.flac": make_track(precision=24)})
    assert "[img]https://ptpimg.me/c1osdy.png[/img] [b]24 bit [color=#2E86C1]44.1[/color] kHz[/b]" in desc


def test_t_description_no_precision_uses_plain_encode_specifics(pinned_cfg):
    desc = t_desc(track_data={"01. Intro.flac": make_track(precision=None)})
    assert "Encode Specifics: 44.1 kHz\n" in desc
    assert "[img]" not in desc


def test_t_description_icons_disabled_uses_text_prefix(monkeypatch, pinned_cfg):
    monkeypatch.setattr(cfg.upload.description, "icons_in_descriptions", False)
    desc = t_desc()
    assert "Encode Specifics: [b]16 bit [color=#2E86C1]44.1[/color] kHz[/b]" in desc


def test_t_description_lossy_comment_included_only_when_cfg_enabled(monkeypatch, pinned_cfg):
    monkeypatch.setattr(cfg.upload.compression, "lma_comment_in_t_desc", True)
    assert "[u]Lossy Notes:[/u]\nSourced from tidal\n\n" in t_desc(lossy_comment="Sourced from tidal")
    monkeypatch.setattr(cfg.upload.compression, "lma_comment_in_t_desc", False)
    assert "Lossy Notes" not in t_desc(lossy_comment="Sourced from tidal")


def test_t_description_tracklist_with_bitrates(monkeypatch, pinned_cfg):
    monkeypatch.setattr(cfg.upload.description, "include_tracklist_in_t_desc", True)
    monkeypatch.setattr(cfg.upload.description, "bitrates_in_t_desc", True)
    desc = t_desc()
    assert "01. Intro [i](3:05)[/i] [941.0kbps]" in desc


def test_t_description_matched_source_url_with_icons(pinned_cfg):
    url = "https://testartist.bandcamp.com/album/testalbum"
    desc = t_desc(source_url=url)
    assert (
        f"[b]Source:[/b] [pad=0|3][url={url}][img]https://ptpimg.me/91oo89.png[/img] Bandcamp[/url][/pad]\n" in desc
    )


def test_t_description_unmatched_source_url_falls_back_to_hostname(pinned_cfg):
    desc = t_desc(source_url="http://example.com/release/1")
    assert "[b]Source:[/b] [url=http://example.com/release/1]example.com[/url]\n" in desc


def test_t_description_non_url_source_url_produces_no_source_line(pinned_cfg):
    assert "Source:" not in t_desc(source_url="not a url")


def test_t_description_no_date_omits_release_date_line(pinned_cfg):
    assert "Released on" not in t_desc(metadata=make_metadata(date=None))


def test_t_description_always_ends_with_footer(pinned_cfg):
    assert t_desc().endswith(FOOTER)


# ---------------------------------------------------------------------------
# generate_torrent / compile_files / collect_logfiles
# ---------------------------------------------------------------------------


def test_generate_torrent_writes_private_torrent_with_announce_and_source(tracker, album_dir):
    tpath, t = generate_torrent(tracker, str(album_dir))
    expected_path = os.path.join(tracker.dot_torrents_dir, "Testartist - Testalbum (2024) [FLAC] - RED.torrent")
    assert tpath == expected_path
    assert os.path.isfile(tpath)
    assert t.private is True
    assert t.source == "RED"

    read_back = Torrent.read(tpath)
    assert read_back.private is True
    assert read_back.source == "RED"
    flat_trackers = [str(url) for tier in read_back.trackers for url in tier]
    assert flat_trackers == [tracker.announce]
    assert read_back.name == "Testartist - Testalbum (2024) [FLAC]"


async def test_compile_files_web_flac_only_has_no_logs(tracker, album_dir):
    _, t = generate_torrent(tracker, str(album_dir))
    files = await compile_files(str(album_dir), t, make_metadata(source="WEB"))
    assert files.torrent_data == t.dump()
    assert files.log_files == []


async def test_compile_files_cd_attaches_logs_but_not_cues(tracker, album_dir):
    (album_dir / "rip.log").write_bytes(b"EAC log content")
    (album_dir / "rip.cue").write_bytes(b"CUE sheet")
    _, t = generate_torrent(tracker, str(album_dir))
    files = await compile_files(str(album_dir), t, make_metadata(source="CD"))
    assert files.log_files == [("rip.log", b"EAC log content")]


async def test_compile_files_non_cd_source_ignores_present_logs(tracker, album_dir):
    # Pin: .log files are only attached for CD rips, even if they exist on disk.
    (album_dir / "rip.log").write_bytes(b"EAC log content")
    _, t = generate_torrent(tracker, str(album_dir))
    files = await compile_files(str(album_dir), t, make_metadata(source="Vinyl"))
    assert files.log_files == []


async def test_collect_logfiles_finds_logs_in_subdirectories(album_dir):
    subdir = album_dir / "CD1"
    subdir.mkdir()
    (subdir / "disc1.log").write_bytes(b"log one")
    logs = await collect_logfiles(str(album_dir))
    assert logs == [("disc1.log", b"log one")]


# ---------------------------------------------------------------------------
# prepare_and_upload
# ---------------------------------------------------------------------------


def new_group_args(tracker, album_dir, metadata=None, **overrides):
    kwargs = {
        "gazelle_site": tracker,
        "path": str(album_dir),
        "group_id": None,
        "metadata": metadata or make_metadata(),
        "cover_url": COVER_URL,
        "track_data": make_track_data(),
        "hybrid": False,
        "lossy_master": False,
        "spectral_urls": None,
        "spectral_ids": None,
        "lossy_comment": None,
        "request_id": None,
    }
    kwargs.update(overrides)
    return kwargs


async def test_prepare_and_upload_new_group_happy_path(tracker, album_dir, pinned_cfg):
    torrent_id, group_id, tpath, torrent = await prepare_and_upload(**new_group_args(tracker, album_dir))

    assert (torrent_id, group_id) == (1001, 2002)
    assert tpath.endswith("Testartist - Testalbum (2024) [FLAC] - RED.torrent")
    assert os.path.isfile(tpath)
    assert isinstance(torrent, Torrent)
    assert tracker.auth_calls == 1

    assert len(tracker.uploads) == 1
    data, files = tracker.uploads[0]
    assert data["title"] == "Testalbum"
    assert "groupid" not in data
    assert files.torrent_data == torrent.dump()
    assert files.log_files == []


async def test_prepare_and_upload_existing_group_sends_groupid_not_title(tracker, album_dir, pinned_cfg):
    await prepare_and_upload(**new_group_args(tracker, album_dir, group_id=555))
    data, _ = tracker.uploads[0]
    assert data["groupid"] == 555
    assert "title" not in data


async def test_prepare_and_upload_existing_group_uses_override_description(tracker, album_dir, pinned_cfg):
    await prepare_and_upload(
        **new_group_args(tracker, album_dir, group_id=555, override_description="Custom transcode desc")
    )
    data, _ = tracker.uploads[0]
    assert data["release_desc"] == "Custom transcode desc"


async def test_prepare_and_upload_cd_rip_attaches_log_files(tracker, album_dir, pinned_cfg):
    (album_dir / "folder.log").write_bytes(b"rip log")
    await prepare_and_upload(**new_group_args(tracker, album_dir, metadata=make_metadata(source="CD")))
    _, files = tracker.uploads[0]
    assert files.log_files == [("folder.log", b"rip log")]


async def test_prepare_and_upload_coerces_string_group_id_to_int(tracker, album_dir, pinned_cfg):
    tracker.upload_result = (7, "42")
    torrent_id, group_id, _, _ = await prepare_and_upload(**new_group_args(tracker, album_dir))
    assert (torrent_id, group_id) == (7, 42)


@pytest.mark.parametrize("falsy_group", [0, None])
async def test_prepare_and_upload_falsy_group_id_becomes_zero(tracker, album_dir, pinned_cfg, falsy_group):
    tracker.upload_result = (7, falsy_group)
    _, group_id, _, _ = await prepare_and_upload(**new_group_args(tracker, album_dir))
    assert group_id == 0


async def test_prepare_and_upload_propagates_upload_error(tracker, album_dir, pinned_cfg):
    tracker.upload_error = UploadError("Site rejected the upload")
    with pytest.raises(UploadError, match="Site rejected the upload"):
        await prepare_and_upload(**new_group_args(tracker, album_dir))
    assert tracker.uploads == []


async def test_prepare_and_upload_propagates_request_error(tracker, album_dir, pinned_cfg):
    tracker.upload_error = RequestError("Site upload failed: dupe (200)")
    with pytest.raises(RequestError, match="dupe"):
        await prepare_and_upload(**new_group_args(tracker, album_dir))
    assert tracker.uploads == []


# ---------------------------------------------------------------------------
# upload_and_report (salmon/uploader/__init__.py)
# ---------------------------------------------------------------------------


class FakeTorrentContent:
    def __init__(self):
        self.comment = None
        self.writes = []

    def write(self, path, overwrite=False):
        self.writes.append((path, overwrite))


class RecordingSeedbox:
    def __init__(self):
        self.tasks = []

    def add_upload_task(self, directory, task_type, is_flac):
        self.tasks.append((directory, task_type, is_flac))


TORRENT_PATH = "/fake/dottorrents/album.torrent"


def install_fakes(monkeypatch, *, error=None, clipboard=False, seedbox=False):
    state = {"prepare_calls": [], "reports": [], "clipboard": [], "content": FakeTorrentContent()}

    async def fake_prepare_and_upload(**kwargs):
        state["prepare_calls"].append(kwargs)
        if error is not None:
            raise error
        return 1001, 2002, TORRENT_PATH, state["content"]

    async def fake_report_lossy_master(*args, **kwargs):
        state["reports"].append((args, kwargs))

    monkeypatch.setattr("salmon.uploader.prepare_and_upload", fake_prepare_and_upload)
    monkeypatch.setattr("salmon.uploader.report_lossy_master", fake_report_lossy_master)
    monkeypatch.setattr("salmon.uploader.pyperclip.copy", lambda text: state["clipboard"].append(text))
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", clipboard)
    monkeypatch.setattr(cfg.upload, "upload_to_seedbox", seedbox)
    return state


def uar_args(tracker, seedbox_uploader, **overrides):
    kwargs = {
        "gazelle_site": tracker,
        "path": "/fake/music/Testartist - Testalbum (2024) [FLAC]",
        "group_id": None,
        "metadata": make_metadata(),
        "cover_url": COVER_URL,
        "track_data": make_track_data(),
        "hybrid": False,
        "lossy_master": False,
        "spectral_urls": None,
        "spectral_ids": None,
        "lossy_comment": None,
        "request_id": None,
        "source_url": None,
        "seedbox_uploader": seedbox_uploader,
        "source": "WEB",
    }
    kwargs.update(overrides)
    return kwargs


async def test_upload_and_report_happy_path_returns_ids_url_and_writes_comment(monkeypatch, fake_tracker):
    state = install_fakes(monkeypatch)
    seedbox = RecordingSeedbox()

    result = await upload_and_report(**uar_args(fake_tracker, seedbox))

    torrent_id, group_id, torrent_path, torrent_content, url = result
    assert (torrent_id, group_id) == (1001, 2002)
    assert torrent_path == TORRENT_PATH
    assert torrent_content is state["content"]
    assert url == "https://redacted.sh/torrents.php?torrentid=1001"
    # The torrent is rewritten with the permalink as its comment.
    assert state["content"].comment == url
    assert state["content"].writes == [(TORRENT_PATH, True)]
    # Not lossy: no report is filed.
    assert state["reports"] == []
    # Seedbox disabled: nothing queued.
    assert seedbox.tasks == []


async def test_upload_and_report_forwards_arguments_to_prepare_and_upload(monkeypatch, fake_tracker):
    state = install_fakes(monkeypatch)
    metadata = make_metadata()
    await upload_and_report(
        **uar_args(fake_tracker, RecordingSeedbox(), group_id=99, metadata=metadata, request_id=1234,
                   source_url="https://example.com/x")
    )
    call = state["prepare_calls"][0]
    assert call["gazelle_site"] is fake_tracker
    assert call["group_id"] == 99
    assert call["metadata"] is metadata
    assert call["request_id"] == 1234
    assert call["source_url"] == "https://example.com/x"
    # No override given: the kwarg is omitted entirely.
    assert "override_description" not in call


async def test_upload_and_report_passes_override_description_when_given(monkeypatch, fake_tracker):
    state = install_fakes(monkeypatch)
    await upload_and_report(**uar_args(fake_tracker, RecordingSeedbox(), override_description="transcode desc"))
    assert state["prepare_calls"][0]["override_description"] == "transcode desc"


async def test_upload_and_report_forwards_explicit_empty_override_description(monkeypatch, fake_tracker):
    state = install_fakes(monkeypatch)
    await upload_and_report(**uar_args(fake_tracker, RecordingSeedbox(), override_description=""))
    assert state["prepare_calls"][0]["override_description"] == ""


async def test_upload_and_report_lossy_true_files_report_with_comment(monkeypatch, fake_tracker):
    state = install_fakes(monkeypatch)
    spectral_urls = {1: ["u1", "u2"]}
    spectral_ids = {1: "01. Intro"}
    await upload_and_report(
        **uar_args(
            fake_tracker,
            RecordingSeedbox(),
            lossy_master=True,
            lossy_comment="Sourced from Tidal",
            spectral_urls=spectral_urls,
            spectral_ids=spectral_ids,
            source_url="https://example.com/x",
        )
    )
    assert len(state["reports"]) == 1
    args, kwargs = state["reports"][0]
    assert args == (fake_tracker, 1001, spectral_urls, spectral_ids, "WEB", "Sourced from Tidal")
    assert kwargs == {"source_url": "https://example.com/x"}


async def test_upload_and_report_override_lossy_comment_wins(monkeypatch, fake_tracker):
    state = install_fakes(monkeypatch)
    await upload_and_report(
        **uar_args(
            fake_tracker,
            RecordingSeedbox(),
            lossy_master=True,
            lossy_comment="original comment",
            override_lossy_comment="Transcode of https://redacted.sh/torrents.php?torrentid=1",
        )
    )
    args, _ = state["reports"][0]
    assert args[5] == "Transcode of https://redacted.sh/torrents.php?torrentid=1"


async def test_upload_and_report_lossy_false_does_not_report(monkeypatch, fake_tracker):
    state = install_fakes(monkeypatch)
    await upload_and_report(
        **uar_args(fake_tracker, RecordingSeedbox(), lossy_master=False, lossy_comment="ignored")
    )
    assert state["reports"] == []


async def test_upload_and_report_queues_seedbox_folder_then_seed_tasks(monkeypatch, fake_tracker):
    install_fakes(monkeypatch, seedbox=True)
    seedbox = RecordingSeedbox()
    args = uar_args(fake_tracker, seedbox)
    await upload_and_report(**args)
    assert seedbox.tasks == [
        (args["path"], "folder", True),
        (TORRENT_PATH, "seed", True),
    ]


async def test_upload_and_report_seedbox_is_flac_false_for_mp3(monkeypatch, fake_tracker):
    install_fakes(monkeypatch, seedbox=True)
    seedbox = RecordingSeedbox()
    await upload_and_report(**uar_args(fake_tracker, seedbox, metadata=make_metadata(format="MP3")))
    assert [task[2] for task in seedbox.tasks] == [False, False]


async def test_upload_and_report_copies_url_to_clipboard_when_enabled(monkeypatch, fake_tracker):
    state = install_fakes(monkeypatch, clipboard=True)
    await upload_and_report(**uar_args(fake_tracker, RecordingSeedbox()))
    assert state["clipboard"] == ["https://redacted.sh/torrents.php?torrentid=1001"]


async def test_upload_and_report_clipboard_disabled_does_not_copy(monkeypatch, fake_tracker):
    state = install_fakes(monkeypatch, clipboard=False)
    await upload_and_report(**uar_args(fake_tracker, RecordingSeedbox()))
    assert state["clipboard"] == []


@pytest.mark.parametrize("error_cls", [UploadError, RequestError])
async def test_upload_and_report_propagates_prepare_errors_without_side_effects(
    monkeypatch, fake_tracker, error_cls
):
    state = install_fakes(monkeypatch, error=error_cls("upload failed"), clipboard=True, seedbox=True)
    seedbox = RecordingSeedbox()
    with pytest.raises(error_cls, match="upload failed"):
        await upload_and_report(**uar_args(fake_tracker, seedbox, lossy_master=True, lossy_comment="c"))
    assert state["reports"] == []
    assert state["clipboard"] == []
    assert seedbox.tasks == []
    assert state["content"].writes == []


# ---------------------------------------------------------------------------
# _prompt_source
# ---------------------------------------------------------------------------


def install_prompt(monkeypatch, responses):
    it = iter(responses)
    calls = []

    async def fake_prompt(*args, **kwargs):
        calls.append((args, kwargs))
        return next(it)

    monkeypatch.setattr("salmon.uploader.click.prompt", fake_prompt)
    return calls


async def test_prompt_source_valid_source_returned_immediately(monkeypatch):
    calls = install_prompt(monkeypatch, ["web"])
    assert await _prompt_source() == "WEB"
    assert len(calls) == 1


async def test_prompt_source_is_case_insensitive(monkeypatch):
    install_prompt(monkeypatch, ["CD"])
    assert await _prompt_source() == "CD"


async def test_prompt_source_invalid_then_valid_reprompts(monkeypatch):
    calls = install_prompt(monkeypatch, ["bogus", "vinyl"])
    assert await _prompt_source() == "Vinyl"
    assert len(calls) == 2


async def test_prompt_source_empty_input_reprompts_until_valid(monkeypatch):
    calls = install_prompt(monkeypatch, ["", "", "sacd"])
    assert await _prompt_source() == "SACD"
    assert len(calls) == 3


@pytest.mark.parametrize("abort_input", ["a", "abort", "A", " Abort "])
async def test_prompt_source_exact_abort_input_aborts(monkeypatch, abort_input):
    install_prompt(monkeypatch, [abort_input])
    with pytest.raises(click.Abort):
        await _prompt_source()


@pytest.mark.parametrize("non_abort", ["aiff", "atmos"])
async def test_prompt_source_other_a_input_reprompts_instead_of_aborting(monkeypatch, non_abort):
    calls = install_prompt(monkeypatch, [non_abort, "web"])
    assert await _prompt_source() == "WEB"
    assert len(calls) == 2
