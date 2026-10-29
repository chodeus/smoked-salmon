"""What this fork's library and link handling add to upstream's: symlinks, covers, and paths as given."""

import hashlib
import io
import os
import struct
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from asyncclick.testing import CliRunner
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

import salmon.checks
import salmon.commands
import salmon.converter
import salmon.tagger
import salmon.tagger.foldername
import salmon.trackers
import salmon.uploader
from salmon import cfg
from salmon.tagger.audio_info import metadata_size

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
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "green").save(buffer, "jpeg")
    (folder / "cover.jpg").write_bytes(buffer.getvalue())
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


def test_an_album_symlinked_into_a_library_is_in_it(dirs, tmp_path) -> None:
    library, _downloads = dirs
    elsewhere = _album(tmp_path / "elsewhere" / "Album")
    (library / "Artist").mkdir()
    (library / "Artist" / "Linked").symlink_to(elsewhere)

    assert cfg.directory.is_library_path(str(library / "Artist" / "Linked"))
    assert cfg.directory.protects(str(library / "Artist" / "Linked"))
    assert not cfg.directory.is_library_path(str(elsewhere))


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


async def _retag(path: str, *_args: Any) -> None:
    """Stands in for tag_files: rewrites a tag in every FLAC of the folder it is given."""
    for root, _dirs, files in os.walk(path):
        for name in files:
            if name.endswith(".flac"):
                tagged = FLAC(os.path.join(root, name))
                tagged["album"] = "Album"
                tagged.save()


async def _rename_files(path: str, *_args: Any) -> None:
    """Stands in for rename_files: renames the tracks of the folder it is given."""
    for name in os.listdir(path):
        if name.endswith(".flac"):
            os.rename(os.path.join(path, name), os.path.join(path, name.replace(" - ", ". ")))


def _run_up(
    monkeypatch,
    album: Path,
    *,
    embed: bool | None = False,
    scene: bool = False,
    cover_url: str | None = None,
    **fakes: Any,
) -> tuple[Any, list[str], list[tuple[str, str]]]:
    """Run `salmon up ALBUM -g 5` with the real staging, tag standardizing, picture strip and folder rename.

    The seams that need audio tools, a network or a reviewer are stubbed, `fakes` replace more of them.
    Returns the result, the folder of every upload, and the (source, output) of every transcode.
    """
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

        def remove_local_copies(self, keep_because: str | None = None) -> None:
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
        "cover": cover_url,
        "genres": [],
    }
    for name, fake in {
        "confirm_group_upload": _returning_async({}),
        "gather_audio_info": _returning({}),
        "check_hybrid": _returning(False),
        "gather_tags": _returning({}),
        "construct_rls_data": _returning_async(rls_data),
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
    # A config with yes_all on would answer the questions the run is meant to ask.
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(cfg.upload.requests, "check_requests", False)
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", False)
    if embed is not None:
        monkeypatch.setattr(cfg.image, "embed_missing_cover", embed)
    monkeypatch.setattr(salmon.trackers, "get_class", lambda _tracker: FakeSite)

    async def run():
        return await CliRunner().invoke(
            salmon.uploader.up,
            [
                str(album),
                "-t",
                "RED",
                "-g",
                "5",
                "-s",
                "WEB",
                "-n",
                "-yyy",
                "--skip-integrity-check",
                "--skip-up",
                *(["--scene"] if scene else []),
            ],
            input="y\n",
        )

    return anyio.run(run), uploads, transcodes


def _bare_album(folder: Path) -> tuple[Path, bytes]:
    """A release whose FLACs have no picture, with a real cover.jpg. Returns the folder and the cover's bytes."""
    folder.mkdir(parents=True)
    _write_flac(folder / "01 - one.flac", title="One", artist="Artist")
    _write_flac(folder / "02 - two.flac", title="Two", artist="Artist")
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "green").save(buffer, "jpeg")
    (folder / "cover.jpg").write_bytes(buffer.getvalue())
    return folder, buffer.getvalue()


def _pictures_at_upload(seen: list[list[int]], files: list[list[str]] | None = None):
    """An upload_and_report that records how many pictures each FLAC has, and the files, when the torrent is made."""

    async def upload_and_report(_site: Any, path: str, *_args: Any, **_kwargs: Any) -> tuple:
        if files is not None:
            files.append(sorted(str(file.relative_to(path)) for file in Path(path).rglob("*") if file.is_file()))
        seen.append(sorted(len(FLAC(file).pictures) for file in Path(path).rglob("*.flac")))
        return 21, 5, "/t.torrent", b"", "https://tracker.test/t"

    return upload_and_report


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


def test_up_embeds_a_sibling_flacs_cover_with_no_cover_url(monkeypatch, dirs) -> None:
    _library, downloads = dirs
    album, cover_bytes = _bare_album(downloads / "Album")
    (album / "cover.jpg").unlink()
    one = FLAC(album / "01 - one.flac")
    picture = Picture()
    picture.type, picture.mime, picture.data = PictureType.COVER_FRONT, "image/jpeg", cover_bytes
    one.add_picture(picture)
    one.save()
    seen: list[list[int]] = []

    result, _uploads, _transcodes = _run_up(monkeypatch, album, embed=None, upload_and_report=_pictures_at_upload(seen))

    assert result.exit_code == 0, result.output
    assert seen[0] == [1, 1]
    assert all(FLAC(file).pictures[0].data == cover_bytes for file in (downloads / RENAMED).glob("*.flac"))


@pytest.mark.parametrize(
    ("strip", "scene", "stripped"),
    [(None, False, True), (False, False, False), (None, True, False)],
    ids=["default", "strip_oversized_pictures off", "scene"],
)
def test_oversized_pictures_are_stripped_from_the_copy(
    monkeypatch, dirs, strip: bool | None, scene: bool, stripped: bool
) -> None:
    library, _downloads = dirs
    album = _album(library / "Album")
    before = _snapshot(library)
    if strip is not None:
        monkeypatch.setattr(cfg.image, "strip_oversized_pictures", strip)
    pictures_when_transcoded: list[int] = []

    async def transcode(path: str, bitrate: str, *_args: Any, output_dir: str | None = None, **_kw: Any) -> str:
        pictures_when_transcoded.append(len(FLAC(next(Path(path).glob("01*one.flac"))).pictures))
        new_path = os.path.join(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [{bitrate}]")
        os.makedirs(new_path)
        return new_path

    result, _uploads, _transcodes = _run_up(monkeypatch, album, scene=scene, transcode_folder=transcode)

    assert result.exit_code == 0, result.output
    assert ("exceed RED's 1 MiB limit" in result.output) is not scene
    assert pictures_when_transcoded == [0 if stripped else 1] * 2
    assert _snapshot(library) == before


@pytest.mark.parametrize("strip", [True, False])
def test_the_upload_rules_see_the_files_after_the_strip(monkeypatch, dirs, strip: bool) -> None:
    _library, downloads = dirs
    album = _album(downloads / "Album")
    monkeypatch.setattr(cfg.image, "strip_oversized_pictures", strip)

    def tag_sizes(path: str) -> dict[str, dict[str, int | None]]:
        files = Path(path).rglob("*.flac")
        return {str(f.relative_to(path)): {"sample rate": 44100, "tag size": metadata_size(FLAC(f))} for f in files}

    result, _uploads, _transcodes = _run_up(
        monkeypatch, album, gather_audio_info=tag_sizes, concat_track_data=lambda _tags, audio_info: audio_info
    )

    assert result.exit_code == 0, result.output
    assert ("2.3.19, a trump reason" in result.output) is not strip


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


async def _same_group(_site, group_id, *_args):
    return group_id


def test_transcode_works_on_the_album_as_given_not_where_a_link_leads(monkeypatch, dirs, tmp_path) -> None:
    library, downloads = dirs
    _album(tmp_path / "elsewhere" / "Album")
    (library / "Artist").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    outputs: list[str | None] = []

    async def transcode_folder(path: str, *_args: Any, output_dir: str | None = None, **_kwargs: Any) -> str:
        outputs.append(output_dir)
        return path

    monkeypatch.setattr(salmon.converter, "transcode_folder", transcode_folder)

    async def run():
        return await CliRunner().invoke(salmon.converter.transcode, [str(library / "Artist" / "Album"), "-b", "V0"])

    result = anyio.run(run)

    assert result.exit_code == 0, result.output
    # Seen in the library, so the output goes under download_directory, not beside the files the link leads to.
    assert len(outputs) == 1
    assert outputs[0] is not None
    assert outputs[0].startswith(str(downloads))
