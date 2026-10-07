"""Embedding, shrinking and saving cover pictures."""

import io
import random
import struct
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

from salmon.tagger import cover
from salmon.tagger.audio_info import metadata_size


@pytest.fixture(autouse=True)
def _lowercase_cover(monkeypatch) -> None:
    monkeypatch.setattr(cover.cfg.upload.formatting, "lowercase_cover", True)


MIB = 1024 * 1024


KIB = 1024


# Stands in for the audio frames, which follow the metadata blocks and must come out of a strip unchanged.
AUDIO = b"\xff\xf8 not really audio " * 1000


def _write_flac(path: Path, *, pictures: tuple[tuple[int, bytes], ...] = (), padding: int = 8 * KIB) -> None:
    """Write a FLAC with fake audio, the given (picture type, data) pictures and padding."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo + AUDIO)
    audio = FLAC(path)
    for picture_type, data in pictures:
        picture = Picture()
        picture.type = picture_type
        picture.mime = "image/jpeg"
        picture.data = data
        audio.add_picture(picture)
    audio.save(padding=lambda _info: padding)


def _image(image_format: str, size: int = 0) -> bytes:
    """A small real image, padded after its end to `size` bytes, which readers ignore."""
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "green").save(buffer, image_format)
    return buffer.getvalue().ljust(size, b"\0")


FRONT = _image("jpeg", 1500 * KIB)


def _png_bytes(
    mode: str, size: tuple[int, int], seed: int, *, known_block: tuple[int, int, int, int] | None = None
) -> bytes:
    """A large random PNG in `mode`, over the limit; `known_block` is set to a known value (alpha 0, or 32768)."""
    width, height = size
    rand = random.Random(seed)
    if mode == "RGBA":
        image = Image.frombytes("RGBA", size, rand.randbytes(width * height * 4))
        if known_block:
            x0, y0, x1, y1 = known_block
            for x in range(x0, x1):
                for y in range(y0, y1):
                    image.putpixel((x, y), (10, 20, 30, 0))
    elif mode == "LA":
        image = Image.frombytes("LA", size, rand.randbytes(width * height * 2))
    elif mode == "P":
        image = Image.frombytes("P", size, rand.randbytes(width * height))
        image.putpalette([i for i in range(256) for _ in range(3)])
        image.info["transparency"] = 5
    elif mode == "I;16":
        image = Image.frombytes("I;16", size, rand.randbytes(width * height * 2))
        if known_block:
            x0, y0, x1, y1 = known_block
            for x in range(x0, x1):
                for y in range(y0, y1):
                    image.putpixel((x, y), 32768)
    else:
        raise ValueError(mode)
    buffer = io.BytesIO()
    image.save(buffer, "png")
    return buffer.getvalue()


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {str(file.relative_to(folder)): file.read_bytes() for file in sorted(folder.rglob("*")) if file.is_file()}


def test_a_failed_cleanup_does_not_hide_the_write_error(tmp_path, monkeypatch) -> None:
    class DiskFull(io.FileIO):
        def write(self, data) -> int:
            raise OSError(28, "No space left on device")

    def cannot_remove(_path) -> None:
        raise PermissionError("cannot remove")

    monkeypatch.setattr(cover, "open", DiskFull, raising=False)
    monkeypatch.setattr(cover.os, "remove", cannot_remove)

    with pytest.raises(OSError, match="No space left"):
        cover._write_whole_file(str(tmp_path / "cover.jpg"), FRONT)


def test_compression_tries_each_quality_afresh() -> None:
    noise = Image.frombytes("RGB", (300, 300), random.Random(0).randbytes(300 * 300 * 3))
    sizes = {}
    for quality in (95, 90):
        buffer = io.BytesIO()
        noise.save(buffer, "jpeg", optimize=True, quality=quality)
        sizes[quality] = len(buffer.getvalue())
    # Too big at quality 95, small enough at 90.
    target = (sizes[95] + sizes[90]) // 2

    data = cover.compress_to_target_size(noise, target)

    assert data is not None
    assert len(data) == sizes[90]
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"


def test_a_cover_that_cannot_be_shrunk_is_not_embedded(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "cover.jpg").write_bytes(_image("jpeg", 2 * MIB))
    _write_flac(tmp_path / "01.flac")
    monkeypatch.setattr(cover, "compress_to_target_size", lambda _image, _target: None)

    cover.compress_pictures(str(tmp_path))

    assert FLAC(tmp_path / "01.flac").pictures == []
    out = capsys.readouterr().out
    assert "Could not shrink" in out


@pytest.mark.parametrize(
    ("mode", "size", "seed"),
    [
        ("RGBA", (1000, 1000), 1),
        ("P", (1100, 1100), 2),
        ("LA", (1000, 1000), 3),
        ("I;16", (1000, 1000), 4),
    ],
)
def test_auto_compress_cover_converts_unusual_modes_before_saving_as_jpeg(tmp_path, mode, size, seed) -> None:
    # RGBA gets a fully transparent 32x32 block; I;16 gets a mid-grey (32768) 32x32 block, both top left.
    known_block = (0, 0, 32, 32) if mode in ("RGBA", "I;16") else None
    cover_bytes = _png_bytes(mode, size, seed, known_block=known_block)
    (tmp_path / "cover.png").write_bytes(cover_bytes)
    assert len(cover_bytes) > MIB
    _write_flac(tmp_path / "01.flac")

    cover.compress_pictures(str(tmp_path))

    audio = FLAC(tmp_path / "01.flac")
    assert len(audio.pictures) == 1
    picture = audio.pictures[0]
    assert picture.type == PictureType.COVER_FRONT
    with Image.open(io.BytesIO(picture.data)) as embedded:
        assert embedded.format == "JPEG"
        # Sampled away from the known block's edge, to allow for JPEG's block-based compression error.
        if mode == "RGBA":
            pixel = embedded.convert("RGB").getpixel((16, 16))
            assert isinstance(pixel, tuple)
            assert all(channel > 240 for channel in pixel)
        if mode == "I;16":
            # A 16-bit value of 32768 scales to 128 in 8 bits; convert("RGB") alone clips it to 255 (white).
            pixel = embedded.convert("L").getpixel((16, 16))
            assert isinstance(pixel, int)
            assert 100 <= pixel <= 156
    size = metadata_size(audio)
    assert size is not None and size <= MIB


def test_a_cover_that_is_not_a_readable_image_is_skipped(tmp_path, capsys) -> None:
    (tmp_path / "cover.png").write_bytes(b"not a real image, just garbage bytes" * 100)
    _write_flac(tmp_path / "01.flac")

    cover.compress_pictures(str(tmp_path))

    audio = FLAC(tmp_path / "01.flac")
    assert audio.pictures == []
    output = capsys.readouterr().out
    assert "Could not read cover file" in output


def test_a_cover_pil_refuses_as_a_decompression_bomb_is_skipped(tmp_path, monkeypatch, capsys) -> None:
    # PIL raises Image.DecompressionBombError, not an OSError, for an image it judges too large to open safely.
    monkeypatch.setattr(cover.Image, "MAX_IMAGE_PIXELS", 10)
    (tmp_path / "cover.png").write_bytes(_image("png"))
    _write_flac(tmp_path / "01.flac")

    cover.compress_pictures(str(tmp_path))

    audio = FLAC(tmp_path / "01.flac")
    assert audio.pictures == []
    output = capsys.readouterr().out
    assert "Could not read cover file" in output


def test_auto_compress_cover_embeds_within_the_limit_counting_the_picture_block(tmp_path) -> None:
    # The image alone fits beside 8 KiB of padding; with the PICTURE block's own fields it would not.
    (tmp_path / "cover.jpg").write_bytes(_image("jpeg", MIB - 8 * KIB - 1))
    _write_flac(tmp_path / "01.flac")

    cover.compress_pictures(str(tmp_path))

    audio = FLAC(tmp_path / "01.flac")
    assert len(audio.pictures) == 1
    size = metadata_size(audio)
    assert size is not None and size <= MIB


def test_a_failed_cover_write_leaves_no_partial_cover(tmp_path, monkeypatch) -> None:
    # A partial cover.jpg would pass for the folder's cover next time, and the embedded one would be stripped.
    _write_flac(tmp_path / "01.flac", pictures=((PictureType.COVER_FRONT, FRONT),))
    before = _snapshot(tmp_path)

    class DiskFull(io.FileIO):
        def write(self, data) -> int:
            super().write(bytes(data)[:1000])
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(cover, "open", DiskFull, raising=False)

    # The fork strips in strip_oversized_pictures, which writes the front cover out first.
    with pytest.raises(OSError, match="No space left"):
        cover.strip_oversized_pictures(str(tmp_path), {"01.flac": {"tag size": 2 * MIB}})

    assert _snapshot(tmp_path) == before


def test_a_taken_temporary_name_raises_rather_than_touching_that_file(tmp_path, monkeypatch) -> None:
    (tmp_path / f".{'0' * 32}.part").write_bytes(b"someone else's")
    monkeypatch.setattr(cover.uuid, "uuid4", lambda: cover.uuid.UUID(int=0))

    with pytest.raises(FileExistsError):
        cover._write_whole_file(str(tmp_path / "cover.jpg"), FRONT)

    assert sorted(file.name for file in tmp_path.iterdir()) == [f".{'0' * 32}.part"]
    assert (tmp_path / f".{'0' * 32}.part").read_bytes() == b"someone else's"
