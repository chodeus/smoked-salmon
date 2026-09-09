"""RED image hosting: a group gets the bare ``/i/`` URL, never RED's per-viewer signed one."""

import html
from pathlib import Path
from typing import Any, ClassVar

import aiohttp
import anyio
import msgspec
from yarl import URL

from salmon import cfg
from salmon.errors import ImageUploadFailed
from salmon.images.base import BaseImageUploader
from salmon.trackers.base import _build_tracker_cookies

BASE_URL = "https://redacted.sh"
AJAX_URL = f"{BASE_URL}/ajax.php"


def bare_image_url(url: str) -> str:
    """Drop RED's per-viewer query credentials from an image URL; other hosts pass through untouched."""
    try:
        parsed = URL(html.unescape(url))
        if parsed.origin() != URL(BASE_URL).origin():
            return url
    except ValueError:
        return url
    return str(parsed.with_query(None).with_fragment(None))


class ImageUploader(BaseImageUploader):
    """Upload images to RED using the configured RED session cookie."""

    _authkey: ClassVar[str | None] = None
    _authkey_session: ClassVar[str | None] = None
    _authkey_lock: ClassVar[anyio.Lock] = anyio.Lock()

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload an image and return its bare RED image URL."""
        self.validate_file(filename)

        red_settings = cfg.tracker.red
        if red_settings is None:
            raise ImageUploadFailed("RED image hosting requires a configured RED session")

        async with await anyio.open_file(filename, "rb") as file_handle:
            file_data = await file_handle.read()

        headers = {"User-Agent": cfg.upload.user_agent}
        # Normalize like tracker requests: a raw cookie with / + : = gets quoted by
        # aiohttp and RED rejects it. Also carries keeplogged when configured.
        cookies = _build_tracker_cookies(red_settings.session, red_settings.keeplogged)

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                # Jar-scope the session cookie to RED so a redirect can't carry it elsewhere
                # (mirrors trackers.base._request).
                session.cookie_jar.update_cookies(cookies, response_url=URL(BASE_URL))
                authkey = await self._get_authkey(session, red_settings.session)
                form = aiohttp.FormData()
                form.add_field("auth", authkey)
                form.add_field("file", file_data, filename=Path(filename).name)
                image_url = await self._upload_image(session, form)
                # The URL is server-controlled; only a RED-origin URL may be handed on as a cover.
                try:
                    image_origin = URL(image_url).origin()
                except ValueError as error:
                    raise ImageUploadFailed(f"RED returned an unusable image URL: {image_url!r}") from error
                if image_origin != URL(BASE_URL).origin():
                    raise ImageUploadFailed(f"RED returned an off-origin image URL; refusing: {image_url!r}")
        except (aiohttp.ClientError, TimeoutError) as error:
            raise ImageUploadFailed(f"Network error: {error}") from error

        return bare_image_url(image_url), None

    async def _get_authkey(self, session: aiohttp.ClientSession, session_cookie: str) -> str:
        """Return the account authkey required by RED's image-upload endpoint."""
        image_uploader_class = type(self)
        async with image_uploader_class._authkey_lock:
            if image_uploader_class._authkey is not None and image_uploader_class._authkey_session == session_cookie:
                return image_uploader_class._authkey

            async with session.get(AJAX_URL, params={"action": "index"}, allow_redirects=False) as response:
                response.raise_for_status()
                payload = await _decode_response(response)

            try:
                authkey = str(payload["response"]["authkey"])
            except (KeyError, TypeError) as error:
                raise ImageUploadFailed("RED did not return an authorization key") from error
            image_uploader_class._authkey = authkey
            image_uploader_class._authkey_session = session_cookie
            return authkey

    @staticmethod
    async def _upload_image(session: aiohttp.ClientSession, form: aiohttp.FormData) -> str:
        """Upload an image and return the URL exactly as RED's response gives it."""
        async with session.post(
            AJAX_URL, params={"action": "upload_image"}, data=form, allow_redirects=False
        ) as response:
            response.raise_for_status()
            payload = await _decode_response(response)

        try:
            return str(payload["response"]["url"])
        except (KeyError, TypeError) as error:
            raise ImageUploadFailed("RED did not return an image URL") from error


async def _decode_response(response: aiohttp.ClientResponse) -> dict[str, Any]:
    """Decode and validate a standard RED AJAX response without logging secrets."""
    try:
        payload = msgspec.json.decode(await response.text())
    except (msgspec.DecodeError, ValueError) as error:
        raise ImageUploadFailed("RED returned an invalid response") from error

    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ImageUploadFailed("RED image request was unsuccessful")
    return payload
