"""Embedded artwork becomes the folder's cover before anything else has to fetch or strip it."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import anyio
from mutagen.id3 import PictureType

from salmon import cfg
from salmon.tagger import cover

# `salmon.checks.integrity` the attribute is a click command; the module needs a direct import.
ig = importlib.import_module("salmon.checks.integrity")


class _FakeFLAC:
    pictures: list = []

    def __init__(self, _path):
        pass


def _fake_flac(monkeypatch, pictures):
    monkeypatch.setattr(_FakeFLAC, "pictures", pictures)
    monkeypatch.setattr(cover, "FLAC", _FakeFLAC)
    monkeypatch.setattr(cfg.upload.formatting, "lowercase_cover", True)


def _front(data=b"jpeg-bytes", mime="image/jpeg"):
    return SimpleNamespace(type=PictureType.COVER_FRONT, mime=mime, data=data)


def test_extract_embedded_cover_writes_the_front_picture(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [SimpleNamespace(type=PictureType.COVER_BACK, mime="image/jpeg", data=b"back"), _front()])

    result = cover.extract_embedded_cover(str(album_dir))

    assert result == str(album_dir / "cover.jpg")
    assert (album_dir / "cover.jpg").read_bytes() == b"jpeg-bytes"


def test_extract_embedded_cover_names_png_pictures_png(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [_front(b"png-bytes", "image/png")])

    assert cover.extract_embedded_cover(str(album_dir)) == str(album_dir / "cover.png")


def test_an_existing_cover_file_wins(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [_front()])
    (album_dir / "Folder.png").write_bytes(b"already there")

    assert cover.extract_embedded_cover(str(album_dir)) == str(album_dir / "Folder.png")
    assert not (album_dir / "cover.jpg").exists()


def test_no_picture_means_no_cover_file(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [])

    assert cover.extract_embedded_cover(str(album_dir)) is None
    assert not any(Path(album_dir).glob("cover.*"))


def test_download_step_prefers_embedded_art_over_the_url(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [_front()])

    async def never(*_args):
        raise AssertionError("nothing should be downloaded while the files carry the cover")

    monkeypatch.setattr(cover, "_download_cover", never)

    result = anyio.run(cover.download_cover_if_nonexistent, str(album_dir), "https://img.example/cover.jpg")

    assert result == (str(album_dir / "cover.jpg"), True)


def test_sanitize_saves_the_embedded_cover_before_re_encoding(tmp_path, monkeypatch) -> None:
    album = tmp_path / "album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"not really flac")
    order: list[str] = []

    monkeypatch.setattr(cover, "extract_embedded_cover", lambda path: order.append(f"extract:{path}"))

    async def fake_process(files, _fn, _label):
        order.append(f"sanitize:{len(files)}")
        return [True]

    monkeypatch.setattr(ig, "process_files", fake_process)

    assert anyio.run(ig.sanitize_integrity, str(album)) is True
    assert order == [f"extract:{album}", "sanitize:1"]
