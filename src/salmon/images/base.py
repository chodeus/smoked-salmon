import functools
import mimetypes
from collections.abc import Awaitable, Callable
from typing import Any

from salmon import dryrun

mimetypes.init()

_UploadFile = Callable[[Any, str], Awaitable[tuple[str, str | None]]]


def _refused_in_dry_run(upload_file: _UploadFile) -> _UploadFile:
    """Wrap an image host's upload_file so it uploads nothing during a dry run."""

    @functools.wraps(upload_file)
    async def guarded(self: "BaseImageUploader", filename: str) -> tuple[str, str | None]:
        dryrun.refuse(f"upload {filename} to {type(self).__module__.rsplit('.', 1)[-1]}")
        return await upload_file(self, filename)

    return guarded


class BaseImageUploader:
    """Base class for image uploaders: subclasses implement the async upload_file, which refuses in a dry run."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Every upload goes through upload_file, whatever it sends its requests through.
        upload_file = cls.__dict__.get("upload_file")
        if upload_file is not None:
            # setattr: an assignment is checked against upload_file's own signature, which the wrapper keeps.
            setattr(cls, "upload_file", _refused_in_dry_run(upload_file))  # noqa: B010

    def validate_file(self, filename: str) -> None:
        """Raise ValueError unless ``filename`` has an image MIME type."""
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type or mime_type.split("/")[0] != "image":
            raise ValueError(f"Unknown image file type {mime_type}")

    async def upload_file(self, filename: str) -> tuple[str, str | None]:
        """Upload an image file and return the URL.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, deletion_url). deletion_url may be None.

        Raises:
            ValueError: If the file is not an image.
            NotImplementedError: If not overridden by subclass.
        """
        self.validate_file(filename)
        raise NotImplementedError("Subclasses must implement upload_file")
