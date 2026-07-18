"""RED image hosting support.

RED image URLs require short-lived credentials to be viewable. Credentials are
cached until RED's returned Unix expiry timestamp, then refreshed for the next
upload.
"""

import time
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlencode

import aiohttp
import anyio
import msgspec

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader

BASE_URL = "https://redacted.sh"
AJAX_URL = f"{BASE_URL}/ajax.php"


class ImageUploader(BaseImageUploader):
    """Upload images to RED using the configured RED session cookie."""

    _image_auth: ClassVar[dict[str, str] | None] = None
    _image_auth_expires_at: ClassVar[float] = 0.0
    _image_auth_session: ClassVar[str | None] = None
    _image_auth_lock: ClassVar[anyio.Lock] = anyio.Lock()

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload an image and return its authenticated RED image URL.

        RED's ``imgauth`` credentials are cached until their returned expiry
        time, then refreshed for a later upload.
        """
        self.validate_file(filename)

        red_settings = cfg.tracker.red
        if red_settings is None:
            raise ImageUploadFailed("RED image hosting requires a configured RED session")

        async with await anyio.open_file(filename, "rb") as file_handle:
            file_data = await file_handle.read()

        form = aiohttp.FormData()
        form.add_field("file", file_data, filename=Path(filename).name)
        headers = {"User-Agent": cfg.upload.user_agent}
        cookies = {"session": red_settings.session}

        try:
            async with aiohttp.ClientSession(headers=headers, cookies=cookies) as session:
                image_url = await self._upload_image(session, form)
                image_auth = await self._get_valid_image_auth(session, red_settings.session)
        except aiohttp.ClientError as error:
            raise ImageUploadFailed(f"Network error: {error}") from error

        return f"{image_url}?{urlencode(image_auth)}", None

    async def _get_valid_image_auth(self, session: aiohttp.ClientSession, session_cookie: str) -> dict[str, str]:
        """Return cached image credentials, refreshing them after expiry or session changes."""
        image_uploader_class = type(self)
        async with image_uploader_class._image_auth_lock:
            if (
                image_uploader_class._image_auth is not None
                and image_uploader_class._image_auth_session == session_cookie
                and time.time() < image_uploader_class._image_auth_expires_at
            ):
                return image_uploader_class._image_auth

            image_auth = await self._get_image_auth(session)
            try:
                image_uploader_class._image_auth_expires_at = float(image_auth["e"])
            except ValueError as error:
                raise ImageUploadFailed("RED returned an invalid image-auth expiry") from error
            image_uploader_class._image_auth = image_auth
            image_uploader_class._image_auth_session = session_cookie
            return image_auth

    @staticmethod
    async def _upload_image(session: aiohttp.ClientSession, form: aiohttp.FormData) -> str:
        """Upload an image and return RED's unauthenticated image URL."""
        async with session.post(AJAX_URL, params={"action": "upload_image"}, data=form) as response:
            response.raise_for_status()
            payload = await _decode_response(response)

        try:
            return str(payload["response"]["url"])
        except (KeyError, TypeError) as error:
            raise ImageUploadFailed("RED did not return an image URL") from error

    @staticmethod
    async def _get_image_auth(session: aiohttp.ClientSession) -> dict[str, str]:
        """Return a newly issued set of RED image-access credentials."""
        async with session.get(AJAX_URL, params={"action": "imgauth"}) as response:
            response.raise_for_status()
            payload = await _decode_response(response)

        try:
            credentials = payload["response"]
            return {key: str(credentials[key]) for key in ("h", "e", "u")}
        except (KeyError, TypeError) as error:
            raise ImageUploadFailed("RED did not return image-access credentials") from error


async def _decode_response(response: aiohttp.ClientResponse) -> dict[str, Any]:
    """Decode and validate a standard RED AJAX response without logging secrets."""
    try:
        payload = msgspec.json.decode(await response.text())
    except (msgspec.DecodeError, ValueError) as error:
        raise ImageUploadFailed("RED returned an invalid response") from error

    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ImageUploadFailed("RED image request was unsuccessful")
    return payload
