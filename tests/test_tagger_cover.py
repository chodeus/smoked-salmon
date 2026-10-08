"""Embedded artwork becomes the folder's cover before anything else has to fetch or strip it."""

import importlib
import io
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from aiohttp import web
from mutagen.id3 import PictureType
from PIL import Image

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


def _image(fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buffer, fmt)
    return buffer.getvalue()


JPEG, PNG, GIF = _image("jpeg"), _image("png"), _image("gif")


def _front(data=JPEG, mime="image/jpeg"):
    return SimpleNamespace(type=PictureType.COVER_FRONT, mime=mime, data=data)


def test_extract_embedded_cover_writes_the_front_picture(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [SimpleNamespace(type=PictureType.COVER_BACK, mime="image/jpeg", data=b"back"), _front()])

    result = cover.extract_embedded_cover(str(album_dir))

    assert result == str(album_dir / "cover.jpg")
    assert (album_dir / "cover.jpg").read_bytes() == JPEG


def test_extract_embedded_cover_names_png_pictures_png(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [_front(PNG, "image/png")])

    result = cover.extract_embedded_cover(str(album_dir))

    assert result == str(album_dir / "cover.png")


def test_an_existing_cover_file_wins(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [_front()])
    (album_dir / "Folder.png").write_bytes(b"already there")

    result = cover.extract_embedded_cover(str(album_dir))

    assert result == str(album_dir / "Folder.png")
    assert not (album_dir / "cover.jpg").exists()


def test_no_picture_means_no_cover_file(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [])

    result = cover.extract_embedded_cover(str(album_dir))

    assert result is None
    assert not any(Path(album_dir).glob("cover.*"))


def test_another_image_format_is_saved_as_png(album_dir, monkeypatch) -> None:
    # A cover file must be JPEG or PNG; upstream's #522 converts the rest instead of skipping them.
    _fake_flac(monkeypatch, [_front(GIF, "image/gif")])

    result = cover.extract_embedded_cover(str(album_dir))

    assert result == str(album_dir / "cover.png")
    assert Image.open(album_dir / "cover.png").format == "PNG"
    assert not (album_dir / "cover.jpg").exists()


def test_the_pictures_own_format_names_the_file_whatever_its_mime_type_says(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [_front(PNG, "image/jpeg")])

    written = cover.extract_embedded_cover(str(album_dir))

    assert written == str(album_dir / "cover.png")


def test_a_16_bit_picture_is_saved_scaled_down_not_white(album_dir, monkeypatch) -> None:
    buffer = io.BytesIO()
    Image.new("I;16", (4, 4), 32768).save(buffer, "TIFF")
    _fake_flac(monkeypatch, [_front(buffer.getvalue(), "image/tiff")])

    written = cover.extract_embedded_cover(str(album_dir))

    assert written is not None
    assert Image.open(written).convert("L").getpixel((0, 0)) == 128


def test_an_unreadable_picture_is_skipped_in_favour_of_a_readable_one(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [_front(b"not an image", "image/webp"), _front(PNG, "IMAGE/PNG")])

    result = cover.extract_embedded_cover(str(album_dir))

    assert result == str(album_dir / "cover.png")
    assert (album_dir / "cover.png").read_bytes() == PNG


def test_only_unreadable_pictures_means_no_cover_file(album_dir, monkeypatch) -> None:
    _fake_flac(monkeypatch, [_front(b"not an image", "image/webp")])

    result = cover.extract_embedded_cover(str(album_dir))

    assert result is None
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

    result = anyio.run(ig.sanitize_integrity, str(album))

    assert result is True
    assert order == [f"extract:{album}", "sanitize:1"]


def test_sanitizing_a_single_flac_saves_the_embedded_cover_first(tmp_path, monkeypatch) -> None:
    album = tmp_path / "album"
    album.mkdir()
    flac = album / "01.flac"
    flac.write_bytes(b"not really flac")
    order: list[str] = []

    monkeypatch.setattr(cover, "extract_embedded_cover", lambda path: order.append(f"extract:{path}"))

    async def fake_sanitize_flac(path):
        order.append(f"sanitize:{Path(path).name}")
        return True

    monkeypatch.setattr(ig, "_sanitize_flac", fake_sanitize_flac)

    result = anyio.run(ig.sanitize_integrity, str(flac))

    assert result is True
    assert order == [f"extract:{album}", "sanitize:01.flac"]


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, "jpeg")
    return buffer.getvalue()


@pytest.fixture
async def cover_server():
    runners: list[web.AppRunner] = []

    async def _serve(handler) -> str:
        app = web.Application()
        app.router.add_get("/cover.jpg", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        runners.append(runner)
        return f"http://127.0.0.1:{runner.addresses[0][1]}/cover.jpg"

    yield _serve
    for runner in runners:
        await runner.cleanup()


async def test_a_cover_download_cut_off_partway_leaves_no_file(tmp_path, monkeypatch, cover_server) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "lowercase_cover", False)
    body = _jpeg()

    async def cut_off(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Length": str(len(body) * 10), "Content-Type": "image/jpeg"})
        await response.prepare(request)
        await response.write(body[:40])
        assert request.transport is not None
        request.transport.close()
        return response

    result = await cover._download_cover(str(tmp_path), await cover_server(cut_off))

    assert result is None
    assert list(tmp_path.iterdir()) == []


async def test_a_downloaded_cover_is_written_whole(tmp_path, monkeypatch, cover_server) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "lowercase_cover", False)
    body = _jpeg()

    async def whole(_request: web.Request) -> web.Response:
        return web.Response(body=body, content_type="image/jpeg")

    result = await cover._download_cover(str(tmp_path), await cover_server(whole))

    assert result == str(tmp_path / "Cover.jpg")
    assert [entry.name for entry in tmp_path.iterdir()] == ["Cover.jpg"]
    assert (tmp_path / "Cover.jpg").read_bytes() == body
