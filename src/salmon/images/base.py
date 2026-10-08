import functools
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from salmon import dryrun

mimetypes.init()

_UploadFile = Callable[[Any, str], Awaitable[tuple[str, str | None]]]

# 5 min total per aiohttp request, including any pool wait; 30 s to connect. Batch items queue before aiohttp.
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=30)


def describe_upload_timeout(e: TimeoutError) -> str:
    """Word a timed-out upload's error with the timeout it actually hit: connecting, or the whole upload."""
    if isinstance(e, aiohttp.ServerTimeoutError):
        return f"Connection to the host timed out after {UPLOAD_TIMEOUT.sock_connect}s"
    return f"Upload timed out after {UPLOAD_TIMEOUT.total}s"


def _refused_in_dry_run(upload_file: _UploadFile) -> _UploadFile:
    """Wrap an image host's upload_file so it uploads nothing during a dry run."""

    @functools.wraps(upload_file)
    async def guarded(self: "BaseImageUploader", filename: str) -> tuple[str, str | None]:
        dryrun.refuse(f"upload {filename} to {self.host}")
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

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    @property
    def host(self) -> str:
        """The host's name, as HOSTS and the config know it: its module's."""
        return type(self).__module__.rsplit(".", 1)[-1]

    @asynccontextmanager
    async def connections(self, limit: int) -> AsyncIterator[None]:
        """Send the uploads made inside this block over one pool of at most `limit` reused connections."""
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=limit), timeout=UPLOAD_TIMEOUT
        ) as session:
            self._session = session
            try:
                yield
            finally:
                self._session = None

    @asynccontextmanager
    async def _http_session(self) -> AsyncIterator[aiohttp.ClientSession]:
        """The session to upload through: the pool of `connections()`, else one for this upload."""
        if self._session is not None:
            yield self._session
        else:
            async with aiohttp.ClientSession(timeout=UPLOAD_TIMEOUT) as session:
                yield session

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
