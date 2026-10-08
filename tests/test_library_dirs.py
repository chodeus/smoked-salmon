"""library_dirs: an album inside one comes out of any salmon run byte-identical, and is never deleted."""

import hashlib
import os
import struct
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

import salmon.checks
import salmon.commands
import salmon.converter
import salmon.tagger
import salmon.tagger.foldername
import salmon.trackers
import salmon.uploader
from salmon import cfg
from salmon.config.validations import Directory
from salmon.errors import AbortAndDeleteFolder, UploadError
from salmon.uploader import staging
from salmon.uploader.spectrals import get_spectrals_path

RENAMED = "Artist - Album (2020) [WEB FLAC]"


def _write_flac(path: Path, **tags: str) -> None:
    """Write a FLAC file with no audio: a STREAMINFO block and the given tags."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    tagged = FLAC(path)
    for key, value in tags.items():
        tagged[key] = value
    tagged.save()


def _album(folder: Path) -> Path:
    """A release that every step of an upload would change: an alias tag, and an oversized embedded picture."""
    (folder / "CD1").mkdir(parents=True)
    # "year" is an alias standardize_tags rewrites to "date", in place.
    _write_flac(folder / "01 - one.flac", title="One", artist="Artist", year="2020")
    _write_flac(folder / "CD1" / "02 - two.flac", title="Two", artist="Artist", year="2020")
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = "image/jpeg"
    picture.data = b"x" * (1024 * 1024)
    tagged = FLAC(folder / "01 - one.flac")
    tagged.add_picture(picture)
    tagged.save()
    (folder / "cover.jpg").write_bytes(b"jpeg")
    # Old mtimes, so a rewrite shows even within the filesystem's timestamp resolution.
    for entry in folder.rglob("*"):
        os.utime(entry, ns=(1_000_000_000, 1_000_000_000))
    return folder


def _snapshot(folder: Path) -> dict[str, tuple[str, int, int]]:
    """Every entry under folder: a hash of its bytes (or "dir"), its mtime and its inode, by relative path."""
    return {
        str(entry.relative_to(folder)): (
            hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else "dir",
            entry.stat().st_mtime_ns,
            entry.stat().st_ino,
        )
        for entry in sorted(folder.rglob("*"))
    }


def _inodes(folder: Path) -> set[int]:
    return {entry.stat().st_ino for entry in folder.rglob("*") if entry.is_file()}


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A library and a download_directory beside it, configured."""
    library = tmp_path / "music"
    library.mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    return library, downloads


# is_library_path and protects


def test_a_library_root_and_everything_under_it_is_a_library_path(dirs) -> None:
    library, _downloads = dirs
    (library / "Artist" / "Album").mkdir(parents=True)

    assert cfg.directory.is_library_path(str(library))
    assert cfg.directory.is_library_path(str(library / "Artist" / "Album"))
    assert cfg.directory.is_library_path(str(library / "Artist" / "Album") + os.sep)


def test_a_sibling_sharing_the_library_prefix_is_not_a_library_path(dirs) -> None:
    library, _downloads = dirs
    sibling = library.parent / "music-old"
    (sibling / "Album").mkdir(parents=True)

    assert not cfg.directory.is_library_path(str(sibling))
    assert not cfg.directory.is_library_path(str(sibling / "Album"))
    assert not cfg.directory.protects(str(sibling / "Album"))


def test_a_symlink_into_the_library_is_a_library_path(dirs, tmp_path) -> None:
    library, _downloads = dirs
    (library / "Album").mkdir()
    link = tmp_path / "shortcut"
    link.symlink_to(library / "Album", target_is_directory=True)

    assert cfg.directory.is_library_path(str(link))
    assert cfg.directory.protects(str(link))


def test_a_library_behind_a_symlinked_entry_is_matched_on_its_real_path(monkeypatch, tmp_path) -> None:
    real = tmp_path / "disk" / "music"
    (real / "Album").mkdir(parents=True)
    (tmp_path / "music").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(tmp_path / "music")])

    assert cfg.directory.is_library_path(str(real / "Album"))


def test_the_filesystem_root_as_a_library_holds_everything(monkeypatch) -> None:
    monkeypatch.setattr(cfg.directory, "library_dirs", [os.path.abspath(os.sep)])

    assert cfg.directory.is_library_path(os.path.join(os.sep, "data", "album"))


def test_a_folder_holding_a_library_is_protected_but_not_a_library_path(dirs) -> None:
    library, _downloads = dirs

    assert not cfg.directory.is_library_path(str(library.parent))
    assert cfg.directory.library_inside(str(library.parent)) == str(library)
    assert cfg.directory.protects(str(library.parent))


def test_without_library_dirs_nothing_is_protected(monkeypatch, tmp_path) -> None:
    directory = Directory(dottorrents_dir=str(tmp_path), download_directory=str(tmp_path))
    assert directory.library_dirs == []
    monkeypatch.setattr(cfg.directory, "library_dirs", [])

    assert not cfg.directory.is_library_path(str(tmp_path))
    assert not cfg.directory.protects(os.path.abspath(os.sep))


# Config validation


def test_a_library_entry_that_is_not_a_directory_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="library_dirs entry is not a valid directory"):
        Directory(
            dottorrents_dir=str(tmp_path), download_directory=str(tmp_path), library_dirs=[str(tmp_path / "missing")]
        )


@pytest.mark.parametrize("field", ["download_directory", "dottorrents_dir", "tmp_dir"])
def test_a_library_entry_holding_a_folder_salmon_writes_in_is_refused(tmp_path, field: str) -> None:
    library = tmp_path / "data"
    inside = library / "torrents"
    inside.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    folders: dict[str, str | None] = {
        "download_directory": str(tmp_path / "elsewhere"),
        "dottorrents_dir": str(tmp_path / "elsewhere"),
        "tmp_dir": None,
        field: str(inside),
    }

    with pytest.raises(ValueError, match=f"must not contain {field}"):
        Directory(
            download_directory=str(folders["download_directory"]),
            dottorrents_dir=str(folders["dottorrents_dir"]),
            tmp_dir=folders["tmp_dir"],
            library_dirs=[str(library)],
        )


@pytest.mark.parametrize("field", ["download_directory", "tmp_dir"])
def test_a_library_entry_inside_a_folder_salmon_deletes_in_is_refused(tmp_path, field: str) -> None:
    # salmon replaces and deletes folders there: a spectrals folder, a conversion, a renamed copy.
    outer = tmp_path / "outer"
    library = outer / "spectrals_Album"
    library.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    folders: dict[str, str | None] = {"download_directory": str(tmp_path / "elsewhere"), "tmp_dir": None}
    folders[field] = str(outer)

    with pytest.raises(ValueError, match=f"must not be inside {field}"):
        Directory(
            dottorrents_dir=str(tmp_path / "elsewhere"),
            download_directory=str(folders["download_directory"]),
            tmp_dir=folders["tmp_dir"],
            library_dirs=[str(library)],
        )


def test_a_library_beside_the_folders_salmon_writes_in_is_accepted(tmp_path) -> None:
    library = tmp_path / "media" / "music"
    library.mkdir(parents=True)
    downloads = tmp_path / "torrents"
    downloads.mkdir()

    directory = Directory(
        dottorrents_dir=str(downloads), download_directory=str(downloads), library_dirs=[str(library)]
    )

    assert directory.library_dirs == [str(library)]


# staged_source


def test_a_library_album_is_staged_as_a_real_copy_whose_rename_goes_to_download_directory(dirs, capsys) -> None:
    library, downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(album)

    with staging.staged_source(str(album), scratch=False) as (staged, rename_into):
        assert rename_into is None
        assert os.path.dirname(os.path.dirname(staged)) == str(downloads / staging.STAGING_DIR)
        assert _inodes(Path(staged)).isdisjoint(_inodes(album))
        Path(staged, "cover.jpg").write_bytes(b"changed")

    assert _snapshot(album) == before
    assert os.listdir(downloads / staging.STAGING_DIR) == []
    out = capsys.readouterr().out
    assert "library_dirs" in out


def test_a_folder_holding_a_library_is_refused(dirs) -> None:
    library, _downloads = dirs

    with (
        pytest.raises(UploadError, match="holds the library folder"),
        staging.staged_source(str(library.parent), scratch=False),
    ):
        pytest.fail("the upload ran on a folder holding a library")


def test_a_library_folder_itself_is_refused(dirs) -> None:
    library, _downloads = dirs

    with (
        pytest.raises(UploadError, match="holds the library folder"),
        staging.staged_source(str(library), scratch=False),
    ):
        pytest.fail("the upload ran on a whole library")


def _link(album: Path, library: Path, how: str) -> None:
    """Share one file of album with the library the way how says."""
    twin = library / "Artist" / "Album"
    twin.mkdir(parents=True)
    if how == "hardlinked file":
        os.link(album / "01 - one.flac", twin / "01 - one.flac")
    elif how == "symlinked file":
        (album / "01 - one.flac").rename(twin / "01 - one.flac")
        (album / "01 - one.flac").symlink_to(twin / "01 - one.flac")
    else:
        (album / "CD1").rename(twin / "CD1")
        (album / "CD1").symlink_to(twin / "CD1")


@pytest.mark.parametrize("how", ["hardlinked file", "symlinked file", "symlinked folder"])
def test_an_album_sharing_files_is_staged_as_a_real_copy(dirs, capsys, how: str) -> None:
    library, downloads = dirs
    album = _album(downloads / "Album")
    _link(album, library, how)
    before = _snapshot(library)

    with staging.staged_source(str(album), scratch=False) as (staged, rename_into):
        assert rename_into is None
        assert os.path.dirname(os.path.dirname(staged)) == str(downloads / staging.STAGING_DIR)
        assert not any(entry.is_symlink() for entry in Path(staged).rglob("*"))
        assert _inodes(Path(staged)).isdisjoint(_inodes(library))
        for flac in Path(staged).rglob("*.flac"):
            flac.write_bytes(b"changed")

    assert _snapshot(library) == before
    out = capsys.readouterr().out
    assert "hardlink or symlink" in out


def test_an_album_sharing_no_file_is_worked_on_in_place(dirs) -> None:
    _library, downloads = dirs
    album = _album(downloads / "Album")

    with staging.staged_source(str(album), scratch=False) as (staged, rename_into):
        assert (staged, rename_into) == (str(album), None)


def test_an_unreadable_folder_counts_as_sharing_files(dirs) -> None:
    from salmon.common.files import shares_files

    _library, downloads = dirs
    album = _album(downloads / "Album")
    (album / "CD1").chmod(0)
    try:
        if os.access(album / "CD1", os.R_OK):
            pytest.skip("permission bits are not enforced for this user")
        assert shares_files(str(album))
    finally:
        (album / "CD1").chmod(0o755)


def test_an_entry_that_vanishes_mid_walk_counts_as_sharing_files(monkeypatch, dirs) -> None:
    from salmon.common import files

    _library, downloads = dirs
    album = _album(downloads / "Album")
    real_lstat = os.lstat

    def vanishing(path, *args, **kwargs):
        if os.path.basename(path) == "01 - one.flac":
            raise FileNotFoundError(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(files.os, "lstat", vanishing)
    shared = files.shares_files(str(album))

    assert shared


def test_an_album_symlinked_into_a_library_is_in_it(dirs, tmp_path) -> None:
    library, _downloads = dirs
    elsewhere = _album(tmp_path / "elsewhere" / "Album")
    (library / "Artist").mkdir()
    (library / "Artist" / "Linked").symlink_to(elsewhere)

    assert cfg.directory.is_library_path(str(library / "Artist" / "Linked"))
    assert cfg.directory.protects(str(library / "Artist" / "Linked"))
    assert not cfg.directory.is_library_path(str(elsewhere))


# salmon up


class FakeSite:
    site_code = "RED"
    site_string = "RED"
    base_url = "https://tracker.test"


def _returning(result: Any = None):
    def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _returning_async(result: Any = None):
    async def fake(*_args, **_kwargs) -> Any:
        return result

    return fake


def _retag(path: str, *_args: Any) -> None:
    """Stands in for tag_files: rewrites a tag in every FLAC of the folder it is given."""
    for root, _dirs, files in os.walk(path):
        for name in files:
            if name.endswith(".flac"):
                tagged = FLAC(os.path.join(root, name))
                tagged["album"] = "Album"
                tagged.save()


def _rename_files(path: str, *_args: Any) -> None:
    """Stands in for rename_files: renames the tracks of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            os.rename(os.path.join(path, name), os.path.join(path, name.replace(" - ", ". ")))


def _run_up(monkeypatch, album: Path, **fakes: Any) -> tuple[Any, list[str], list[tuple[str, str]]]:
    """`salmon up ALBUM -g 5` with real staging and renames, `fakes` for more seams: (result, uploads, transcodes)."""
    uploads: list[str] = []
    transcodes: list[tuple[str, str]] = []

    async def upload_and_report(_site: Any, path: str, *_args: Any, **_kwargs: Any) -> tuple:
        uploads.append(path)
        return 21, 5, "/t.torrent", b"", "https://tracker.test/t"

    async def transcode(path: str, bitrate: str, *_args: Any, output_dir: str | None = None, **_kw: Any) -> str:
        new_path = os.path.join(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bitrate}]")
        os.makedirs(new_path)
        transcodes.append((path, new_path))
        return new_path

    class FakeUploadManager:
        async def execute_upload(self) -> None:
            pass

    rls_data = {
        "format": "FLAC",
        "encoding": "Lossless",
        "artists": [("Artist", "main")],
        "title": "Album",
        "catno": "CAT1",
    }
    metadata = {
        **rls_data,
        "source": "WEB",
        "year": 2020,
        "edition_title": None,
        "scene": False,
        "cover": None,
        "genres": [],
    }
    for name, fake in {
        "confirm_group_upload": _returning_async({}),
        "gather_audio_info": _returning({}),
        "check_hybrid": _returning(False),
        "gather_tags": _returning({}),
        "construct_rls_data": _returning(rls_data),
        "mqa_test": _returning_async(),
        "check_spectrals": _returning_async((False, None)),
        "get_metadata": _returning_async((metadata, None)),
        "review_metadata_with_ai": _returning_async(metadata),
        "tag_files": _retag,
        "check_tags": _returning_async({}),
        "rename_files": _rename_files,
        "check_folder_structure": _returning_async(),
        "concat_track_data": _returning({"01. one.flac": {"sample rate": 44100}}),
        "resolve_cover_url": _returning_async((True, None)),
        "print_torrents": _returning_async({}),
        "recheck_edition": _same_group,
        "UploadManager": FakeUploadManager,
        "transcode_folder": transcode,
        "upload_and_report": upload_and_report,
        **fakes,
    }.items():
        monkeypatch.setattr(salmon.uploader, name, fake)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    # -yyy sets yes_all: patched, so it is put back after the test.
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(cfg.upload.requests, "check_requests", False)
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(cfg.image, "auto_compress_cover", False)
    monkeypatch.setattr(salmon.trackers, "get_class", lambda _tracker: FakeSite)

    async def run():
        return await CliRunner().invoke(
            salmon.uploader.up,
            [str(album), "-t", "RED", "-g", "5", "-s", "WEB", "-n", "-yyy", "--skip-integrity-check", "--skip-up"],
            input="y\n",
        )

    return anyio.run(run), uploads, transcodes


@pytest.mark.parametrize("remove_source_dir", [False, True])
@pytest.mark.parametrize("hardlinks", [True, False])
def test_up_leaves_a_library_album_byte_identical_and_uploads_a_copy(
    monkeypatch, dirs, hardlinks: bool, remove_source_dir: bool
) -> None:
    library, downloads = dirs
    album = _album(library / "Artist" / "Album")
    before = _snapshot(library)
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", remove_source_dir)

    result, uploads, transcodes = _run_up(monkeypatch, album)

    assert result.exit_code == 0, result.output
    assert _snapshot(library) == before
    # The upload and both transcodes worked on the renamed copy, which stays in download_directory to seed.
    copy = downloads / RENAMED
    assert uploads == [str(copy), f"{copy} [320]", f"{copy} [V0]"]
    assert [source for source, _output in transcodes] == [str(copy)] * 2
    assert set(os.listdir(downloads)) == {staging.STAGING_DIR, RENAMED, f"{RENAMED} [320]", f"{RENAMED} [V0]"}
    assert os.listdir(downloads / staging.STAGING_DIR) == []
    # Every change the run makes landed in the copy: the retag, the alias rewrite, the renames.
    one = FLAC(copy / "01. one.flac")
    assert (one["album"], one["date"]) == (["Album"], ["2020"])
    assert "year" not in one
    assert FLAC(copy / "CD1" / "02 - two.flac")["album"] == ["Album"]
    assert _inodes(copy).isdisjoint(_inodes(library))
    assert all(os.stat(file).st_nlink == 1 for file in library.rglob("*") if file.is_file())


@pytest.mark.parametrize("how", ["hardlinked file", "symlinked file", "symlinked folder"])
def test_up_leaves_an_album_sharing_files_and_what_it_shares_byte_identical(monkeypatch, dirs, how: str) -> None:
    library, downloads = dirs
    album = _album(downloads / "Album")
    _link(album, library, how)
    before = _snapshot(library), _snapshot(album)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)

    result, uploads, _transcodes = _run_up(monkeypatch, album)

    assert result.exit_code == 0, result.output
    assert (_snapshot(library), _snapshot(album)) == before
    assert uploads[0] == str(downloads / RENAMED)
    assert _inodes(downloads / RENAMED).isdisjoint(_inodes(library))


@pytest.mark.parametrize("how", ["hardlinked file", "symlinked file", "symlinked folder"])
def test_up_never_replaces_an_album_sharing_files_that_already_has_the_new_name(monkeypatch, dirs, how: str) -> None:
    library, downloads = dirs
    album = _album(downloads / RENAMED)
    _link(album, library, how)
    before = _snapshot(library), _snapshot(album), _inodes(album)

    result, uploads, _transcodes = _run_up(monkeypatch, album)

    assert (_snapshot(library), _snapshot(album), _inodes(album)) == before
    assert uploads == []
    assert f"Not replacing {downloads / RENAMED}" in str(result.exception)


def test_up_on_an_album_symlinked_into_a_library_leaves_its_target_byte_identical(monkeypatch, dirs, tmp_path) -> None:
    library, downloads = dirs
    elsewhere = _album(tmp_path / "elsewhere" / "Album")
    (library / "Artist").mkdir()
    (library / "Artist" / "Linked").symlink_to(elsewhere)
    before = _snapshot(elsewhere)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)

    result, uploads, _transcodes = _run_up(monkeypatch, library / "Artist" / "Linked")

    assert result.exit_code == 0, result.output
    assert _snapshot(elsewhere) == before
    assert (library / "Artist" / "Linked").is_symlink()
    assert uploads[0] == str(downloads / RENAMED)


@pytest.mark.parametrize("when", ["before the rename", "after the rename"])
def test_abort_and_delete_keeps_the_library_album_and_deletes_the_copy(monkeypatch, dirs, when: str) -> None:
    library, downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(library)

    async def delete_before(*_args: Any, **_kwargs: Any) -> None:
        raise AbortAndDeleteFolder

    def delete_after(*_args: Any) -> None:
        raise AbortAndDeleteFolder

    # concat_track_data is the first step on the renamed copy, after edit_metadata returns it.
    fakes = {"check_spectrals": delete_before} if when == "before the rename" else {"concat_track_data": delete_after}
    monkeypatch.setattr(cfg.upload, "windows_use_recycle_bin", False)

    result, uploads, _transcodes = _run_up(monkeypatch, album, **fakes)

    assert result.exit_code == 0, result.output
    assert uploads == []
    assert _snapshot(library) == before
    assert f"{album} is kept" in result.output
    assert "Deleted folder" in result.output
    # What was deleted is the copy: nothing of this run is left in download_directory.
    assert os.listdir(downloads) == [staging.STAGING_DIR]
    assert os.listdir(downloads / staging.STAGING_DIR) == []


def test_abort_and_delete_never_deletes_a_folder_in_a_library(monkeypatch, dirs) -> None:
    # Defence in depth: the folder the upload works on is a copy, but if it ever were a library album, it is kept.
    library, _downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(library)
    monkeypatch.setattr(salmon.uploader, "staged_source", _not_staged)

    async def delete(*_args: Any, **_kwargs: Any) -> None:
        raise AbortAndDeleteFolder

    result, _uploads, _transcodes = _run_up(monkeypatch, album, standardize_tags=_returning(), check_spectrals=delete)

    assert result.exit_code == 0, result.output
    assert "Not deleting" in result.output
    assert _snapshot(library) == before


class _not_staged:
    """A staged_source that hands the upload the folder itself."""

    def __init__(self, path: str, scratch: bool) -> None:
        self.path = path

    def __enter__(self) -> tuple[str, None]:
        return self.path, None

    def __exit__(self, *_exc: object) -> None:
        return None


# rename_folder


def _metadata() -> dict[str, Any]:
    return {
        "scene": False,
        "artists": [("Artist", "main")],
        "title": "Album",
        "year": 2020,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
    }


@pytest.mark.parametrize("hardlinks", [True, False])
def test_rename_folder_never_hardlinks_or_removes_a_library_album(monkeypatch, dirs, hardlinks: bool) -> None:
    library, downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(library)
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))

    new_path = salmon.tagger.foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    assert new_path == str(downloads / RENAMED)
    assert _snapshot(library) == before
    assert _inodes(Path(new_path)).isdisjoint(_inodes(library))


def test_rename_folder_never_replaces_a_library_with_the_renamed_folder(monkeypatch, tmp_path) -> None:
    # The config refuses a library inside download_directory; rename_folder still never replaces one.
    downloads = tmp_path / "downloads"
    library = downloads / RENAMED
    _album(library / "Album")
    before = _snapshot(library)
    source = _album(tmp_path / "seeding" / "Album")
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))

    with pytest.raises(UploadError, match="library_dirs"):
        salmon.tagger.foldername.rename_folder(str(source), _metadata(), auto_rename=True, check=False)

    assert _snapshot(library) == before


# Other commands that change a folder


def test_tag_works_on_a_copy_renamed_into_download_directory(monkeypatch, dirs) -> None:
    library, downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(library)
    metadata = {**_metadata(), "cover": None}
    for name, fake in {
        "gather_tags": _returning({}),
        "gather_audio_info": _returning({}),
        "construct_rls_data": _returning({}),
        "get_metadata": _returning_async((metadata, None)),
        "review_metadata_with_ai": _returning_async(metadata),
        "tag_files": _retag,
        "download_cover_if_nonexistent": _returning_async(),
        "check_tags": _returning_async({}),
        "rename_files": _rename_files,
        "check_folder_structure": _returning_async(),
    }.items():
        monkeypatch.setattr(salmon.tagger, name, fake)
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", True)
    monkeypatch.setattr(cfg.upload, "yes_all", True)

    async def run():
        return await CliRunner().invoke(salmon.tagger.tag, [str(album), "-s", "WEB", "-n"])

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert _snapshot(library) == before
    assert set(os.listdir(downloads)) == {staging.STAGING_DIR, RENAMED}
    one = FLAC(downloads / RENAMED / "01. one.flac")
    assert (one["album"], one["date"]) == (["Album"], ["2020"])


def test_compress_refuses_a_library_album(monkeypatch, dirs) -> None:
    library, _downloads = dirs
    album = _album(library / "Album")
    recompressed: list[str] = []

    async def fake_recompress_path(path: str, files=None) -> None:
        recompressed.append(path)

    monkeypatch.setattr(salmon.commands, "recompress_path", fake_recompress_path)

    async def run():
        return await CliRunner().invoke(salmon.commands.compress, [str(album)])

    result = anyio.run(run)

    assert result.exit_code != 0
    assert "library_dirs" in result.output
    assert recompressed == []


@pytest.mark.parametrize("how", ["hardlinked file", "symlinked file", "symlinked folder"])
def test_compress_refuses_an_album_sharing_files(monkeypatch, dirs, how: str) -> None:
    library, downloads = dirs
    album = _album(downloads / "Album")
    _link(album, library, how)
    recompressed: list[str] = []

    async def fake_recompress_path(path: str, files=None) -> None:
        recompressed.append(path)

    monkeypatch.setattr(salmon.commands, "recompress_path", fake_recompress_path)

    async def run():
        return await CliRunner().invoke(salmon.commands.compress, [str(album)])

    result = anyio.run(run)

    assert result.exit_code != 0
    assert "hardlink or symlink" in result.output
    assert recompressed == []


def test_check_integrity_does_not_offer_to_sanitize_a_file_hardlinked_into_a_library(monkeypatch, dirs) -> None:
    library, downloads = dirs
    album = _album(downloads / "Album")
    _link(album, library, "hardlinked file")
    sanitized: list[str] = []

    async def sanitize(path: str) -> None:
        sanitized.append(path)

    integrity = sys.modules["salmon.checks.integrity"]
    monkeypatch.setattr(integrity, "check_integrity", _returning_async(integrity.IntegrityResult(passed=False)))
    monkeypatch.setattr(integrity, "sanitize_and_verify", sanitize)

    async def run():
        return await CliRunner().invoke(salmon.checks.check, ["integrity", str(album / "01 - one.flac")], input="y\n")

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert "Not offering to sanitize" in result.output
    assert sanitized == []


@pytest.mark.parametrize("target", ["folder", "file"])
def test_check_integrity_does_not_offer_to_sanitize_a_library_album(monkeypatch, dirs, target: str) -> None:
    library, _downloads = dirs
    album = _album(library / "Album")
    path = album if target == "folder" else album / "01 - one.flac"
    sanitized: list[str] = []

    async def sanitize(path: str) -> None:
        sanitized.append(path)

    # salmon.checks.integrity is the command; the module is only reachable through sys.modules.
    integrity = sys.modules["salmon.checks.integrity"]
    monkeypatch.setattr(integrity, "check_integrity", _returning_async(integrity.IntegrityResult(passed=False)))
    monkeypatch.setattr(integrity, "sanitize_and_verify", sanitize)

    async def run():
        return await CliRunner().invoke(salmon.checks.check, ["integrity", str(path)], input="y\n")

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert "Not offering to sanitize" in result.output
    assert sanitized == []


@pytest.mark.parametrize("command", ["transcode", "downconv"])
def test_conversions_of_library_albums_go_to_their_own_folders_in_download_directory(
    monkeypatch, dirs, tmp_path, command: str
) -> None:
    library, downloads = dirs
    # A second library with the same name, on another "disk".
    other = tmp_path / "disk2" / "music"
    other.mkdir(parents=True)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library), str(other)])
    albums = [
        _album(library / "Album"),
        _album(library / "A" / "Hits"),
        _album(library / "B" / "Hits"),
        _album(other / "A" / "Hits"),
    ]
    output_dirs: list[str | None] = []

    async def convert(_path: str, *_args: Any, output_dir: str | None = None, **_kwargs: Any) -> None:
        output_dirs.append(output_dir)

    monkeypatch.setattr(salmon.converter, "transcode_folder", convert)
    monkeypatch.setattr(salmon.converter, "convert_folder", convert)

    for album in albums:
        args = [str(album), "-b", "V0"] if command == "transcode" else [str(album)]

        async def run(args=args):
            return await CliRunner().invoke(getattr(salmon.converter, command), args)

        result = anyio.run(run)
        assert result.exit_code == 0, result.output

    # The album's resolved parent path is mirrored, so no two of the three "Hits" convert into the same folder.
    # A drive letter (Windows) becomes a folder of its own; POSIX has none.
    mirrored: list[str] = []
    for album in albums:
        parent = album.resolve().parent
        drive = [parent.drive.rstrip(":")] if parent.drive else []
        mirrored.append(str(downloads.joinpath(*drive, *parent.parts[1:])))
    assert output_dirs == mirrored
    assert len(set(output_dirs)) == 4


def test_conversions_outside_a_library_still_go_beside_the_source(monkeypatch, dirs, tmp_path) -> None:
    album = _album(tmp_path / "seeding" / "Album")
    output_dirs: list[str | None] = []

    async def convert(_path: str, *_args: Any, output_dir: str | None = None, **_kwargs: Any) -> None:
        output_dirs.append(output_dir)

    monkeypatch.setattr(salmon.converter, "convert_folder", convert)

    async def run():
        return await CliRunner().invoke(salmon.converter.downconv, [str(album)])

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert output_dirs == [None]


def test_spectrals_of_a_library_album_are_made_outside_it(dirs, tmp_path) -> None:
    library, downloads = dirs

    one, two = get_spectrals_path(str(library / "A" / "Album")), get_spectrals_path(str(library / "B" / "Album"))

    assert os.path.dirname(one) == os.path.dirname(two) == str(downloads)
    assert os.path.basename(one).startswith("spectrals_Album ")
    assert one != two
    assert get_spectrals_path(str(tmp_path / "seeding" / "Album")) == str(tmp_path / "seeding" / "Album" / "Spectrals")


def test_an_album_path_walks_dot_dot_after_a_link_as_the_filesystem_does(dirs, tmp_path, monkeypatch) -> None:
    from salmon.common.files import AlbumPath

    library, downloads = dirs
    _album(library / "Album")
    (library / "Artist").mkdir()
    (downloads / "link").symlink_to(library / "Artist", target_is_directory=True)
    (library / "Artist" / "Linked").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    monkeypatch.chdir(downloads)

    walked = AlbumPath(exists=True).convert(os.path.join("link", "..", "Album"), None, None)
    kept = AlbumPath().convert(os.path.join("link", "..", "Artist", "Linked"), None, None)

    assert walked == os.path.realpath(library / "Album")
    assert cfg.directory.is_library_path(walked)
    assert kept == os.path.join(os.path.realpath(library), "Artist", "Linked")


def test_checkspecs_works_on_the_album_as_given_not_where_a_link_leads(monkeypatch, dirs, tmp_path) -> None:
    library, _downloads = dirs
    _album(tmp_path / "elsewhere" / "Album")
    (library / "Artist").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    checked: list[str] = []

    class Site(FakeSite):
        async def api_call(self, *_args: Any, **_kwargs: Any) -> dict:
            return {"torrent": {"filePath": "Album", "media": "WEB"}}

    async def spectral_check(_site: Any, path: str, *_args: Any) -> None:
        checked.append(path)

    monkeypatch.setattr(salmon.trackers, "validate_tracker", _returning_async("RED"))
    monkeypatch.setattr(salmon.trackers, "get_class", lambda _tracker: Site)
    monkeypatch.setattr(salmon.commands, "gather_audio_info", _returning({}))
    monkeypatch.setattr(salmon.commands, "post_upload_spectral_check", spectral_check)

    async def run():
        return await CliRunner().invoke(salmon.commands.checkspecs, [str(library / "Artist"), "-i", "1"])

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    assert checked == [str(library / "Artist" / "Album")]
    assert cfg.directory.protects(checked[0])


def test_rename_folder_never_follows_a_symlink_into_a_library(monkeypatch, dirs, tmp_path) -> None:
    # A folder name that is a symlink in download_directory leads into the library (names never hold a "/" here).
    library, downloads = dirs
    (downloads / "link").symlink_to(library, target_is_directory=True)
    before = _snapshot(library)
    source = _album(tmp_path / "seeding" / "Album")
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning("link"))

    with pytest.raises(UploadError, match="library_dirs"):
        salmon.tagger.foldername.rename_folder(str(source), _metadata(), auto_rename=True, check=False)

    assert _snapshot(library) == before


@pytest.mark.parametrize("converter", ["transcode_folder", "convert_folder"])
def test_conversions_never_follow_a_symlink_into_a_library(monkeypatch, dirs, converter: str) -> None:
    # download_directory/music, where a library album converts into, is a symlink to the library itself.
    library, downloads = dirs
    (downloads / "music").symlink_to(library, target_is_directory=True)
    album = _album(library / "A" / "Hits")
    (library / "A" / "Hits [MP3 V0]").mkdir()
    before = _snapshot(library)

    async def run() -> None:
        if converter == "transcode_folder":
            await salmon.converter.transcode_folder(str(album), "V0", output_dir=str(downloads / "music" / "A"))
        else:
            await salmon.converter.convert_folder(str(album), output_dir=str(downloads / "music" / "A"))

    with pytest.raises(UploadError, match="library_dirs"):
        anyio.run(run)

    assert _snapshot(library) == before


@pytest.mark.parametrize("converter", ["transcode_folder", "convert_folder"])
def test_conversions_never_go_into_a_folder_holding_a_library(monkeypatch, dirs, converter: str) -> None:
    # The config refuses a library inside download_directory; the converters still check what they write into.
    _library, downloads = dirs
    album = _album(downloads.parent / "seeding" / "Hits")
    if converter == "transcode_folder":
        destination = sys.modules["salmon.converter.transcoding"]._build_output_path(str(album), "V0", str(downloads))
    else:
        destination = sys.modules["salmon.converter.downconverting"]._build_output_path(
            str(album), 16, None, str(downloads)
        )
    inner = Path(destination) / "music"
    _album(inner / "Album")
    before = _snapshot(inner)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(inner)])

    async def run() -> None:
        if converter == "transcode_folder":
            await salmon.converter.transcode_folder(str(album), "V0", output_dir=str(downloads))
        else:
            await salmon.converter.convert_folder(str(album), output_dir=str(downloads))

    with pytest.raises(UploadError, match="library_dirs"):
        anyio.run(run)

    assert _snapshot(inner) == before


def test_conversions_from_two_windows_shares_go_to_different_folders(monkeypatch, tmp_path) -> None:
    # \\server\music-1 and \\server\music1 are two shares: squashing the anchor to word characters merged them.
    monkeypatch.setattr(salmon.converter, "Path", PureWindowsPath)
    monkeypatch.setattr(salmon.converter.os.path, "realpath", lambda path: path)
    directory = SimpleNamespace(is_library_path=lambda _path: True, download_directory=str(tmp_path))
    monkeypatch.setattr(salmon.converter, "cfg", SimpleNamespace(directory=directory))

    first = salmon.converter.conversion_output_dir(r"\\server\music-1\A\Album")
    second = salmon.converter.conversion_output_dir(r"\\server\music1\A\Album")

    assert first == os.path.join(str(tmp_path), "UNC", "server", "music-1", "A")
    assert second == os.path.join(str(tmp_path), "UNC", "server", "music1", "A")


async def _same_group(_site, group_id, *_args):
    return group_id
