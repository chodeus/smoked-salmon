"""RED image hosting: a group gets the bare ``/i/`` URL, never RED's per-viewer signed one."""

import html
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp
import anyio
import msgspec
from yarl import URL

from salmon import cfg
from salmon.errors import ImageUploadFailed, RequestError
from salmon.images.base import BaseImageUploader
from salmon.trackers.base import _safe_response_excerpt
from salmon.trackers.red import RedApi

BASE_URL = "https://redacted.sh"


def bare_image_url(url: str) -> str:
    """Drop RED's per-viewer query credentials, and any userinfo, from an image URL; other hosts pass through."""
    try:
        parsed = URL(html.unescape(url))
        if parsed.origin() != URL(BASE_URL).origin():
            return url
    except ValueError:
        return url
    return str(parsed.with_user(None).with_query(None).with_fragment(None))


class ImageUploader(BaseImageUploader):
    """Upload images to RED with the tracker's API key, or its session cookie and authkey without one."""

    async def upload_file(self, filename: str) -> tuple[str, None]:
        """Upload an image and return its bare RED image URL."""
        self.validate_file(filename)
        if cfg.tracker.red is None:
            raise ImageUploadFailed("RED image hosting requires a configured RED session")

        async with await anyio.open_file(filename, "rb") as file_handle:
            file_data = await file_handle.read()

        # A RED client, so these requests spend RED's rate limit like any other.
        site = RedApi()
        use_api_key = bool(site.api_key)
        try:
            form = aiohttp.FormData()
            if not use_api_key:
                await site.ensure_authenticated()
                form.add_field("auth", site.authkey)
            form.add_field("file", file_data, filename=Path(filename).name)
            resp = await site._request(
                "POST",
                f"{site.base_url}/ajax.php",
                params={"action": "upload_image"},
                data=form,
                prefer_api_key=use_api_key,
                needs_authkey=not use_api_key,
                # An image takes RED longer than a page; a timeout here is an unknown outcome.
                timeout_secs=30,
            )
            # Decoded while the client still knows its credentials, so an echoed one is masked.
            payload = _decode_response(resp.text, site._scrub)
        except RequestError as error:
            # Not chained: the original message isn't scrubbed, and a traceback would print it.
            raise ImageUploadFailed(
                f"RED image upload failed: {_safe_response_excerpt(site._scrub(str(error)))}"
            ) from None
        finally:
            await site.close()

        try:
            image_url = str(payload["response"]["url"])
        except (KeyError, TypeError) as error:
            raise ImageUploadFailed("RED did not return an image URL") from error
        # The URL is server-controlled; only a RED-origin URL may be handed on as a cover.
        try:
            parsed = URL(image_url)
            image_origin = parsed.origin()
        except ValueError as error:
            raise ImageUploadFailed("RED returned an unusable image URL") from error
        # Printed and stored downstream, so a URL with userinfo is refused, and not shown.
        if parsed.user or parsed.password:
            raise ImageUploadFailed("RED returned an image URL carrying credentials; refusing it")
        if image_origin != URL(BASE_URL).origin():
            raise ImageUploadFailed(f"RED returned an image URL on {image_origin.host}; refusing it")
        return bare_image_url(image_url), None


def _decode_response(text: str, scrub: Callable[[str], str]) -> dict[str, Any]:
    """Decode and validate a standard RED AJAX response without logging secrets."""
    try:
        payload = msgspec.json.decode(text)
    except (msgspec.DecodeError, ValueError) as error:
        raise ImageUploadFailed("RED returned an invalid response") from error

    if not isinstance(payload, dict) or payload.get("status") != "success":
        reason = _rejection_reason(payload, scrub)
        raise ImageUploadFailed(
            f"RED rejected the request: {reason}" if reason else "RED image request was unsuccessful"
        )
    return payload


def _rejection_reason(payload: Any, scrub: Callable[[str], str]) -> str | None:
    """RED's own error text, redacted and capped like any other tracker response, or None."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if error is None:
        return None
    text = error if isinstance(error, str) else msgspec.json.encode(error).decode()
    return _safe_response_excerpt(scrub(text))
