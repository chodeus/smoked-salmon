"""Torrent file name normalization."""

import os
import shutil
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from torf import Torrent

from salmon import cfg
from salmon.config.validations import Upload
from salmon.errors import UploadRefusedError
from salmon.uploader.upload import _normalize_torrent_names, generate_torrent

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi

COMPOSED_NAME = "Café.flac"
DECOMPOSED_NAME = unicodedata.normalize("NFD", COMPOSED_NAME)


class FakeGazelleApi:
    def __init__(self, dot_torrents_dir: str) -> None:
        self.announce = "https://example.com/announce"
        self.dot_torrents_dir = dot_torrents_dir
        self.site_string = "TEST"


def _make_album(tmp_path: Path) -> Path:
    album = tmp_path / "Album"
    album.mkdir()
    # Named in the decomposed (NFD) form, so NFC normalization has something to change.
    (album / DECOMPOSED_NAME).write_bytes(b"not really flac data")
    return album


def _file_names(t) -> list[str]:
    return [f.parts[-1] for f in t.files]


def test_torrent_name_normalization_nfc(tmp_path: Path) -> None:
    album = _make_album(tmp_path)
    gazelle_site = FakeGazelleApi(str(tmp_path))

    original = cfg.upload.torrent_name_normalization
    try:
        cfg.upload.torrent_name_normalization = "NFC"
        _tpath, t = generate_torrent(cast("BaseGazelleApi", cast("object", gazelle_site)), str(album))
    finally:
        cfg.upload.torrent_name_normalization = original

    names = _file_names(t)
    assert names == [unicodedata.normalize("NFC", DECOMPOSED_NAME)]
    for name in names:
        assert unicodedata.is_normalized("NFC", name)
    # decomposed form must be gone
    assert DECOMPOSED_NAME not in names


def test_torrent_name_normalization_nfd(tmp_path: Path) -> None:
    album = tmp_path / "Album2"
    album.mkdir()
    composed = unicodedata.normalize("NFC", COMPOSED_NAME)
    (album / composed).write_bytes(b"not really flac data")
    gazelle_site = FakeGazelleApi(str(tmp_path))

    original = cfg.upload.torrent_name_normalization
    try:
        cfg.upload.torrent_name_normalization = "NFD"
        _tpath, t = generate_torrent(cast("BaseGazelleApi", cast("object", gazelle_site)), str(album))
    finally:
        cfg.upload.torrent_name_normalization = original

    names = _file_names(t)
    assert names == [unicodedata.normalize("NFD", composed)]
    for name in names:
        assert unicodedata.is_normalized("NFD", name)


def test_torrent_name_normalization_default_leaves_names_untouched(tmp_path: Path) -> None:
    album = _make_album(tmp_path)
    gazelle_site = FakeGazelleApi(str(tmp_path))

    assert cfg.upload.torrent_name_normalization == ""
    _tpath, t = generate_torrent(cast("BaseGazelleApi", cast("object", gazelle_site)), str(album))

    names = _file_names(t)
    assert names == [DECOMPOSED_NAME]


def test_normalize_false_names_the_files_as_on_disk(tmp_path: Path, monkeypatch) -> None:
    album = _make_album(tmp_path)
    monkeypatch.setattr(cfg.upload, "torrent_name_normalization", "NFC")
    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(tmp_path))))

    _tpath, t = generate_torrent(gazelle_site, str(album), normalize=False)

    assert _file_names(t) == [DECOMPOSED_NAME]


def test_a_normalized_torrent_no_longer_reads_the_files_on_disk(tmp_path: Path, monkeypatch) -> None:
    album = _make_album(tmp_path)
    monkeypatch.setattr(cfg.upload, "torrent_name_normalization", "NFC")
    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(tmp_path))))
    _tpath, t = generate_torrent(gazelle_site, str(album))
    shutil.rmtree(album)

    dumped = t.dump()

    assert dumped


def test_a_watch_folder_that_takes_the_torrent_at_once_does_not_break_the_upload(tmp_path: Path, monkeypatch) -> None:
    album = _make_album(tmp_path)
    monkeypatch.setattr(cfg.upload, "torrent_name_normalization", "NFC")
    write = Torrent.write

    def write_then_import(self, filepath, **kwargs) -> None:
        write(self, filepath, **kwargs)
        os.remove(filepath)

    monkeypatch.setattr(Torrent, "write", write_then_import)
    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleApi(str(tmp_path))))

    _tpath, t = generate_torrent(gazelle_site, str(album))

    assert _file_names(t) == [unicodedata.normalize("NFC", DECOMPOSED_NAME)]


def test_two_files_that_normalize_to_one_path_are_refused() -> None:
    """Built by hand: a normalization-insensitive file system (APFS) cannot hold both names."""
    files = [{"length": 1, "path": ["CD1", COMPOSED_NAME]}, {"length": 1, "path": ["CD1", DECOMPOSED_NAME]}]
    t = SimpleNamespace(metainfo={"info": {"name": "Album", "files": files}})

    with pytest.raises(UploadRefusedError, match="CD1/Café.flac once NFC-normalized"):
        _normalize_torrent_names(cast("Torrent", cast("object", t)), "NFC")


def test_torrent_name_normalization_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="torrent_name_normalization"):
        Upload(torrent_name_normalization="nfc")
