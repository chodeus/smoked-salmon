import contextlib
import io
import os
import re
import uuid
from typing import IO, cast

import aiohttp
import asyncclick as click
import humanfriendly
from mutagen import PaddingInfo
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from PIL import Image

from salmon import cfg
from salmon.common import get_audio_files
from salmon.constants import TAG_TRUMP_SIZE

# A cover is a few MiB: this stops a wrong URL from filling memory.
_MAX_COVER_BYTES = 25 * 1024 * 1024
_COVER_FILE = re.compile(r"^(cover|folder)\.(jpe?g|png)$", re.IGNORECASE)


def _existing_cover(path: str) -> str | None:
    for filename in os.listdir(path):
        if _COVER_FILE.match(filename):
            return os.path.join(path, filename)
    return None


def get_cover_from_path(path):
    """Search a folder for a cover image, return its path."""
    cover = _existing_cover(path)
    if cover is None:
        click.secho(f"Did not find a cover in path {path}", fg="red")
    return cover


def _write_picture(path: str, picture) -> str | None:
    """Save an embedded picture as the folder's cover file (JPEG or PNG) and return its path; None if unreadable."""
    cover = _as_cover_file(picture)
    if cover is None:
        return None
    extension, data = cover
    stem = "cover" if cfg.upload.formatting.lowercase_cover else "Cover"
    cover_path = os.path.join(path, f"{stem}.{extension}")
    # A partial cover file would pass for the folder's cover on the next run.
    _write_whole_file(cover_path, data)
    click.secho(f"Extracted cover to: {cover_path}", fg="green")
    return cover_path


def _as_cover_file(picture: Picture) -> tuple[str, bytes] | None:
    """(extension, bytes) to save an embedded picture as a cover: JPEG or PNG as is, else PNG; None if unreadable."""
    try:
        with Image.open(io.BytesIO(picture.data)) as image:
            if image.format in ("JPEG", "PNG"):
                image.load()
                return ("jpg" if image.format == "JPEG" else "png"), picture.data
            buffer = io.BytesIO()
            _eight_bit(image).convert("RGBA").save(buffer, "png")
            return "png", buffer.getvalue()
    except Exception:
        return None


def _write_whole_file(dest: str, data: bytes) -> None:
    """Write a file through a new temporary file beside it, so a failed write never leaves part of it at dest."""
    partial = os.path.join(os.path.dirname(dest), f".{uuid.uuid4().hex}.part")
    # "x" claims a new name: a file already there raises instead of being truncated, and is never removed.
    with open(partial, "xb"):
        pass
    try:
        with open(partial, "wb") as file:
            file.write(data)
        os.replace(partial, dest)
    except BaseException:
        # The write's own error is the one to report, not a failed cleanup.
        with contextlib.suppress(OSError):
            os.remove(partial)
        raise


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """Convert to RGB for JPEG: transparency flattened onto white, 16/32-bit integer modes scaled to 8 bits first."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return _eight_bit(image).convert("RGB")


def _eight_bit(image: Image.Image) -> Image.Image:
    """A 16/32-bit integer mode scaled to 8 bits (convert alone clips it to white); any other as it is."""
    if image.mode not in ("I", "I;16", "I;16B", "I;16L"):
        return image
    image = image.convert("I")
    low, high = cast("tuple[int, int]", image.getextrema())
    # Scaled as 16-bit: a 32-bit value past that range would clip to white, so it is refused.
    if low < 0 or high > 65535:
        raise ValueError(f"pixel values {low}..{high} do not fit 16 bits")
    return image.point(lambda v: v / 256).convert("L")


def extract_embedded_cover(path: str) -> str | None:
    """Save the first embedded front cover as the folder's cover file; an existing cover file wins."""
    existing = _existing_cover(path)
    if existing:
        return existing
    for filename in get_audio_files(path):
        if not filename.lower().endswith(".flac"):
            continue
        try:
            pictures = FLAC(os.path.join(path, filename)).pictures
        except Exception:
            continue
        for picture in pictures:
            if picture.type == PictureType.COVER_FRONT and picture.data:
                written = _write_picture(path, picture)
                if written:
                    return written
    return None


async def download_cover_if_nonexistent(path: str, cover_url: str | None) -> tuple[str | None, bool | None]:
    """Download cover if not already present in folder.

    Args:
        path: Source folder path.
        cover_url: URL for cover image to download.

    Returns:
        Tuple of (cover_path, was_downloaded). Both None if failed.
    """
    # use local file if matches filter
    cover_path = get_cover_from_path(path)
    if cover_path:
        click.secho(f"\nUsing existing cover image found: {cover_path}...", fg="yellow")
        return cover_path, False
    # the files usually carry the store's artwork already
    cover_path = extract_embedded_cover(path)
    if cover_path:
        click.secho(f"\nUsing the cover embedded in the files: {cover_path}...", fg="yellow")
        return cover_path, True
    # use url provided
    if cover_url:
        click.secho("\nDownloading Cover Image...", fg="yellow")
        cover_path = await _download_cover(path, cover_url)
        if cover_path:
            return cover_path, True
    click.secho("\nNo existing Cover Image found in Source Folder, no Cover Image downloaded", fg="red")
    return None, None


def _is_valid_cover(cover_path: str | IO[bytes]) -> bool:
    """Check if the file at cover_path is a valid JPEG or PNG image.

    Args:
        cover_path: Path to the image file.

    Returns:
        True if the file is a valid JPEG or PNG image.
    """
    try:
        mime = Image.open(cover_path).get_format_mimetype()
    except Exception:
        return False
    return mime in ("image/jpeg", "image/png")


async def _download_cover(path: str, cover_url: str) -> str | None:
    """Download cover image from URL.

    Args:
        path: Directory to save the cover.
        cover_url: URL to download from.

    Returns:
        Path to downloaded cover or None on failure.
    """
    ext = os.path.splitext(cover_url)[1]
    c = "c" if cfg.upload.formatting.lowercase_cover else "C"
    headers = {"User-Agent": "smoked-salmon-v1"}
    cover_image_filename = c + "over" + ext
    cover_path = os.path.join(path, cover_image_filename)

    timeout = aiohttp.ClientTimeout(total=30)
    data = bytearray()
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(cover_url, headers=headers) as response,
        ):
            if response.status >= 400:
                click.secho(f"\nFailed to download cover image (ERROR {response.status})", fg="red")
                return None

            async for chunk in response.content.iter_chunked(64 * 1024):
                data += chunk
                if len(data) > _MAX_COVER_BYTES:
                    click.secho(f"\nFailed to download cover image (ERROR over {_MAX_COVER_BYTES} bytes)", fg="red")
                    return None
    except (aiohttp.ClientError, TimeoutError) as e:
        click.secho(f"\nFailed to download cover image (ERROR {e or type(e).__name__})", fg="red")
        return None

    if not _is_valid_cover(io.BytesIO(data)):
        click.secho("\nFailed to download cover image (ERROR file is not an image [JPEG, PNG])", fg="red")
        return None
    # Written whole: a download cut off partway would pass for the folder's cover on the next run.
    _write_whole_file(cover_path, bytes(data))

    click.secho(f"Cover image downloaded: {cover_image_filename} ", fg="yellow")
    return cover_path


def compress_to_target_size(image, target_size):
    quality = 95

    buffer = io.BytesIO()

    while True:
        # Each attempt replaces the last one, or the sizes add up and no quality ever fits.
        buffer.seek(0)
        buffer.truncate()
        image.save(buffer, "jpeg", optimize=True, quality=quality)

        file_size = len(buffer.getvalue())

        if file_size <= target_size:
            print(f"Successfully compressed to {humanfriendly.format_size(file_size, binary=True)}")
            return buffer.getvalue()

        quality -= 5

        if quality <= 75:
            print("Quality too low, cannot compress further!")
            break


def get_8kib_padding(info: PaddingInfo):
    return humanfriendly.parse_size("8KiB")


def strip_oversized_pictures(path: str, track_data: dict) -> list[str]:
    """Drop pictures and padding from FLACs whose tag block is a trump reason; returns the files it rewrote."""
    stripped = []
    for filename, track in track_data.items():
        size = track.get("tag size")
        if not filename.lower().endswith(".flac") or not size or size <= TAG_TRUMP_SIZE:
            continue
        try:
            audio = FLAC(os.path.join(path, filename))
        except Exception as error:
            click.secho(f"{filename}: could not read the FLAC metadata ({type(error).__name__}); left as is.", fg="red")
            continue
        pictures = [picture for picture in audio.pictures if picture.data]
        # Front covers first: the first picture that saves is the copy kept.
        pictures.sort(key=lambda picture: picture.type != PictureType.COVER_FRONT)
        if pictures and not _existing_cover(path) and not any(_write_picture(path, p) for p in pictures):
            # The embedded pictures are the only copy of the artwork: never strip them.
            click.secho(
                f"{filename}: pictures and padding exceed 1 MiB (a trump reason), but left as they are: "
                "the embedded artwork could not be read as an image to keep.",
                fg="red",
            )
            continue
        click.secho(
            f"{filename}: {humanfriendly.format_size(size, binary=True)} of pictures and padding exceeds 1 MiB "
            "(a trump reason); removing them.",
            fg="yellow",
        )
        audio.clear_pictures()
        audio.save(padding=get_8kib_padding)
        stripped.append(filename)
    return stripped


def compress_pictures(path):
    """Embed the folder's cover into FLACs that carry no picture, resized to stay under the trump threshold."""
    cover_file = get_cover_from_path(path)
    for filename in get_audio_files(path):
        if not filename.lower().endswith(".flac"):
            continue
        click.secho(f"Processing file: {filename}", fg="blue")
        audio = FLAC(os.path.join(path, filename))
        if audio.pictures:
            click.secho("Existing covers meet size requirements", fg="bright_white")
            continue
        click.secho("Attempting to add external cover...", fg="magenta")
        if not cover_file:
            click.secho("No cover file found!", fg="red")
            continue

        with open(cover_file, "rb") as c:
            data = c.read()

        max_picture_block_size = TAG_TRUMP_SIZE - humanfriendly.parse_size("8KiB")

        # The PICTURE block's own fields (MIME type, dimensions, lengths) count against the limit too, so
        # the image gets what is left once they are written with no data.
        picture = Picture()
        try:
            # Decoded, not just identified: a truncated image still opens.
            with Image.open(cover_file) as image:
                image.load()
                picture.mime = image.get_format_mimetype()
        except (OSError, ValueError, Image.DecompressionBombError) as e:
            click.secho(f"Could not read cover file {cover_file} as an image ({e}); leaving it out.", fg="red")
            continue

        if len(data) <= max_picture_block_size - len(picture.write()):
            click.secho(
                f"Cover size ({humanfriendly.format_size(len(data), binary=True)}) within limit",
                fg="bright_green",
            )
        else:
            click.secho(
                f"Resizing oversized cover ({humanfriendly.format_size(len(data), binary=True)})...",
                fg="yellow",
            )
            picture.mime = "image/jpeg"
            try:
                image = Image.open(cover_file)
                image.thumbnail((1000, 1000))
                image = _flatten_to_rgb(image)
                data = compress_to_target_size(image, max_picture_block_size - len(picture.write()))
            except (OSError, ValueError, Image.DecompressionBombError) as e:
                click.secho(f"Could not convert cover file {cover_file} to a JPEG ({e}); leaving it out.", fg="red")
                continue
            if data is None:
                click.secho(f"Could not shrink {cover_file} enough to embed it; leaving it out.", fg="red")
                continue

        picture.data = data
        picture.type = PictureType.COVER_FRONT
        audio.add_picture(picture)
        audio.save(padding=get_8kib_padding)
        click.secho(f"Saved {filename} with optimized cover", fg="bright_green")
