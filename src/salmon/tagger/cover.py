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

# A cover is a few MiB: this stops a wrong URL from filling memory.
_MAX_COVER_BYTES = 25 * 1024 * 1024
# 8000x8000: far past any store's artwork, well short of Pillow's decompression-bomb limit.
_MAX_COVER_PIXELS = 64_000_000
_COVER_FILE = re.compile(r"^(cover|folder)\.(jpe?g|png)$", re.IGNORECASE)
# RED allows at most 1 MiB of embedded pictures plus padding per file.
MAX_PICTURES_AND_PADDING = humanfriendly.parse_size("1MiB")


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
    # use url provided
    if cover_url:
        click.secho("\nDownloading Cover Image...", fg="yellow")
        cover_path = await _download_cover(path, cover_url)
        if cover_path:
            return cover_path, True
    # fall back to an embedded FLAC front cover
    cover_path = extract_embedded_cover(path)
    if cover_path:
        return cover_path, True
    click.secho("\nNo existing Cover Image found in Source Folder, no Cover Image downloaded", fg="red")
    return None, None


def _is_valid_cover(cover_path: str | IO[bytes]) -> bool:
    """Whether cover_path, a path or an open binary file, holds a JPEG or PNG image of a cover's size."""
    try:
        with Image.open(cover_path) as image:
            mime = image.get_format_mimetype()
            # Read from the header: a small file can claim pixels that would take gigabytes to load.
            if image.width * image.height > _MAX_COVER_PIXELS:
                return False
            # Every pixel: a download cut off with no Content-Length still has a valid header.
            image.load()
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
        # By type: an aiohttp error can repeat the URL, and a cover URL can be signed.
        click.secho(f"\nFailed to download cover image (ERROR {type(e).__name__})", fg="red")
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


def pictures_and_padding_size(audio: FLAC) -> int:
    """Get the bytes a FLAC spends on embedded pictures and padding, which RED's 1 MiB limit counts."""
    padding_size = sum(block.length for block in audio.metadata_blocks if block.code == 1)
    return padding_size + sum(len(picture.write()) for picture in audio.pictures)


def find_oversized_pictures(path: str) -> dict[str, int]:
    """Find the FLACs in a folder whose embedded pictures plus padding exceed RED's 1 MiB limit.

    Args:
        path: The release folder.

    Returns:
        The size of the pictures plus padding, by file path relative to the folder.
    """
    oversized = {}
    for filename in get_audio_files(path):
        if not filename.lower().endswith(".flac"):
            continue
        size = pictures_and_padding_size(FLAC(os.path.join(path, filename)))
        if size > MAX_PICTURES_AND_PADDING:
            oversized[filename] = size
    return oversized


def _strip_pictures(path: str, audio: FLAC, cover_file: str | None) -> str | None:
    """Remove a FLAC's pictures and padding, keeping 8 KiB of padding.

    Its artwork is written to the folder first if the folder has no cover file: its front cover, else the first other
    picture that reads. If none can be saved as a JPEG or PNG file, the FLAC is left as it is, so the only copy of the
    artwork is not lost.

    Args:
        path: The release folder.
        audio: The FLAC to strip.
        cover_file: The folder's cover file, or None if it has none.

    Returns:
        The folder's cover file afterwards, or None if it still has none.
    """
    if not cover_file:
        # Front covers first: the first picture that saves is the copy kept.
        pictures = sorted((p for p in audio.pictures if p.data), key=lambda p: p.type != PictureType.COVER_FRONT)
        for picture in pictures:
            cover_file = _write_picture(path, picture)
            if cover_file:
                break
        if pictures and not cover_file:
            click.secho(
                f"Left {audio.filename} as it is: its artwork could not be read as an image to keep it.", fg="red"
            )
            return None

    audio.clear_pictures()
    audio.save(padding=get_8kib_padding)
    return cover_file


def check_embedded_pictures(path: str) -> None:
    """Strip the FLACs whose embedded pictures plus padding exceed RED's 1 MiB limit, or only warn about them.

    Stripping removes every embedded picture and all but 8 KiB of padding, keeping the front cover as the
    folder's cover file if it has none. With image.strip_oversized_pictures off, the files are only listed.

    Args:
        path: The release folder, which must be safe to change.
    """
    oversized = find_oversized_pictures(path)
    if not oversized:
        return
    click.secho(
        "\nEmbedded pictures plus padding exceed RED's 1 MiB limit in these files:",
        fg="yellow",
        bold=True,
    )
    for filename, size in oversized.items():
        excess = humanfriendly.format_size(size - MAX_PICTURES_AND_PADDING, binary=True)
        click.secho(f"  {filename}: {humanfriendly.format_size(size, binary=True)} ({excess} over)", fg="yellow")

    if not cfg.image.strip_oversized_pictures:
        click.secho("Leaving them as they are: strip_oversized_pictures is off.", fg="yellow")
        return

    cover_file = get_cover_from_path(path)
    if cover_file and not _is_valid_cover(cover_file):
        # Not a copy of the artwork: the embedded pictures may be the only one.
        click.secho(f"Leaving them as they are: {cover_file} could not be read as a JPEG or PNG image.", fg="red")
        return
    stripped = 0
    for filename in oversized:
        audio = FLAC(os.path.join(path, filename))
        cover_file = _strip_pictures(path, audio, cover_file)
        stripped += not audio.pictures
    click.secho(f"Stripped {stripped} file(s) to no embedded pictures and 8 KiB of padding.", fg="green")


def has_flac_without_picture(path: str) -> bool:
    """Whether any FLAC in the folder has no embedded picture."""
    return any(
        not FLAC(os.path.join(path, filename)).pictures
        for filename in get_audio_files(path)
        if filename.lower().endswith(".flac")
    )


def _prepare_cover(cover_file: str) -> tuple[str, bytes] | None:
    """Read a cover file and, if it is too big to embed beside 8 KiB of padding, shrink it to a JPEG that fits.

    Returns:
        The MIME type and bytes to embed, or None (after saying why) if the cover cannot be read or shrunk enough.
    """
    try:
        with open(cover_file, "rb") as c:
            data = c.read()
    except OSError as e:
        click.secho(f"Could not read cover file {cover_file} as an image ({e}); leaving it out.", fg="red")
        return None

    max_picture_block_size = MAX_PICTURES_AND_PADDING - humanfriendly.parse_size("8KiB")

    # The PICTURE block's own fields (MIME type, dimensions, lengths) count against the limit too, so
    # the image gets what is left once they are written with no data.
    picture = Picture()
    try:
        # Decoded, not just identified: a truncated image still opens.
        with Image.open(cover_file) as opened:
            opened.load()
            mime = opened.get_format_mimetype() or ""
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        click.secho(f"Could not read cover file {cover_file} as an image ({e}); leaving it out.", fg="red")
        return None
    picture.mime = mime

    if len(data) <= max_picture_block_size - len(picture.write()):
        return mime, data

    click.secho(f"Resizing oversized cover ({humanfriendly.format_size(len(data), binary=True)})...", fg="yellow")
    picture.mime = "image/jpeg"
    try:
        with Image.open(cover_file) as image:
            image.thumbnail((1000, 1000))
            image = _flatten_to_rgb(image)
            shrunk = compress_to_target_size(image, max_picture_block_size - len(picture.write()))
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        click.secho(f"Could not convert cover file {cover_file} to a JPEG ({e}); leaving it out.", fg="red")
        return None
    if shrunk is None:
        click.secho(f"Could not shrink {cover_file} enough to embed it; leaving it out.", fg="red")
        return None
    return "image/jpeg", shrunk


def embed_missing_cover(path: str, cover_file: str | None = None) -> None:
    """Embed the folder's cover in every FLAC that has no picture, shrunk to fit under RED's 1 MiB limit.

    A FLAC that has a picture is left as it is, whatever its size (that is `check_embedded_pictures`'s job).
    The cover file is never written. With no cover file, or one that cannot be read or shrunk enough, the FLAC
    is left as it is and a line says so once. The cover is read and shrunk once for the whole folder.

    Args:
        path: The release folder, which must be safe to change.
        cover_file: The cover to embed, if it is not one `get_cover_from_path` finds (a downloaded one can
            have a query string in its name). Defaults to the folder's cover.
    """
    prepared: tuple[str, bytes] | None = None
    looked = False
    for filename in get_audio_files(path):
        if not filename.lower().endswith(".flac"):
            continue
        audio = FLAC(os.path.join(path, filename))
        if audio.pictures:
            continue
        if not looked:
            looked = True
            cover_file = cover_file or get_cover_from_path(path)
            if cover_file:
                prepared = _prepare_cover(cover_file)
            else:
                click.secho("No cover file to embed: FLACs with no picture are left as they are.", fg="yellow")
        if prepared is None:
            continue

        picture = Picture()
        picture.mime, picture.data = prepared
        picture.type = PictureType.COVER_FRONT
        audio.add_picture(picture)
        audio.save(padding=get_8kib_padding)
        click.secho(f"Embedded the cover in {filename}", fg="bright_green")
