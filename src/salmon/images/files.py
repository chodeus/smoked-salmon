"""Whether a file is an image, judged by its content and never by its name."""

import os
from typing import Final

from PIL import Image, UnidentifiedImageError

# The formats the image hosts take, as Pillow names them (MPO is a JPEG that some cameras and phones write).
IMAGE_FORMATS: Final = frozenset({"JPEG", "MPO", "PNG", "GIF", "WEBP"})
# A file bigger than this is not sent (the hosts' own limits differ and are not known to salmon), and an image with
# more pixels than this is not decoded: both bound the memory a check or an upload takes.
MAX_IMAGE_BYTES: Final = 20 * 1024 * 1024
MAX_IMAGE_PIXELS: Final = 50_000_000


class NotAnImage(ValueError):
    """A file that is not an image salmon web may send; the message says why, without the file's name."""


def check_image(path: str) -> str:
    """Return the Pillow format; raise NotAnImage unless it is a JPEG, PNG, GIF or WebP whose first frame decodes."""
    size = os.stat(path).st_size
    if size == 0:
        raise NotAnImage("it is empty")
    if size > MAX_IMAGE_BYTES:
        raise NotAnImage(f"it is bigger than {MAX_IMAGE_BYTES // 1024 // 1024} MiB")
    try:
        with Image.open(path) as image:
            found = image.format
            if found not in IMAGE_FORMATS:
                raise NotAnImage("it is not a JPEG, PNG, GIF or WebP image")
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise NotAnImage("it has too many pixels")
            image.verify()
        # verify() does not read a JPEG's data: decode the whole image, which fails on one that is cut short.
        with Image.open(path) as image:
            image.load()
    except NotAnImage:
        raise
    except (UnidentifiedImageError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise NotAnImage("it is not a JPEG, PNG, GIF or WebP image, or it is corrupt") from None
    except OSError as error:
        # Pillow reports a cut-short image as an OSError ("image file is truncated"); a failure to read the file is
        # one too, and is the caller's.
        if os.access(path, os.R_OK):
            raise NotAnImage("it is cut short or corrupt") from error
        raise
    return found
