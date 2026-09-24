import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import anyio
import msgspec
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.errors import ImageUploadFailed, RequestFailedError
from salmon.images import HOSTS, red
from salmon.images.base import BaseImageUploader
from salmon.trackers.base import HttpResponse


class _FakeRed:
    """Stands in for RedApi: records requests and answers upload_image with ``payload``."""

    base_url = "https://redacted.sh"
    made: ClassVar[list["_FakeRed"]] = []
    payload: ClassVar[dict] = {}

    def __init__(self) -> None:
        self.authkey: str | None = None
        self.calls: list[tuple[str, str, dict]] = []
        self.form: dict[str, object] = {}
        self.closed = False
        _FakeRed.made.append(self)

    async def ensure_authenticated(self) -> None:
        self.calls.append(("auth", "", {}))
        self.authkey = "account-authkey"

    async def _request(self, method: str, url: str, params: dict, data) -> HttpResponse:
        self.calls.append((method, url, params))
        # aiohttp.FormData keeps (options, headers, value) per field; recorded so tests see what was posted.
        self.form = {options["name"]: value for options, _headers, value in data._fields}
        return HttpResponse(text=msgspec.json.encode(self.payload).decode(), url=url, status=200)

    async def close(self) -> None:
        self.closed = True


def _patch_red_env(monkeypatch, payload: dict | None = None):
    """Swap in the fake RED client and stub config for a test."""
    monkeypatch.setattr(_FakeRed, "made", [])
    monkeypatch.setattr(
        _FakeRed, "payload", payload or {"status": "success", "response": {"url": "https://redacted.sh/i/image.png"}}
    )
    monkeypatch.setattr(red, "RedApi", _FakeRed)
    monkeypatch.setattr(
        red,
        "cfg",
        SimpleNamespace(
            tracker=SimpleNamespace(red=SimpleNamespace(session="red-session", keeplogged=None)),
            upload=SimpleNamespace(user_agent="salmon-test"),
        ),
    )


def test_red_is_registered_as_an_image_uploader() -> None:
    assert HOSTS["red"] is red
    assert issubclass(red.ImageUploader, BaseImageUploader)


def test_red_returns_the_bare_image_url(monkeypatch, tmp_path) -> None:
    # RED signs image URLs per viewer itself; the uploader's credentials must never be stored.
    _patch_red_env(
        monkeypatch,
        {"status": "success", "response": {"url": "https://redacted.sh/i/image.png?h=hash&e=1700000000&u=12345"}},
    )
    image = tmp_path / "image.png"
    image.write_bytes(b"png-data")

    result = anyio.run(red.ImageUploader().upload_file, str(image))
    assert result == ("https://redacted.sh/i/image.png", None)
    [site] = _FakeRed.made
    # Through the RED client, so both requests spend RED's rate limit, and its pool is closed.
    assert site.calls == [
        ("auth", "", {}),
        ("POST", "https://redacted.sh/ajax.php", {"action": "upload_image"}),
    ]
    assert site.form == {"auth": "account-authkey", "file": b"png-data"}
    assert site.closed


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://redacted.sh/i/a.jpg?h=hash&e=1700000000&u=12345", "https://redacted.sh/i/a.jpg"),
        ("https://redacted.sh/i/a.jpg?h=hash&amp;e=1700000000&amp;u=12345", "https://redacted.sh/i/a.jpg"),
        ("https://redacted.sh/t/thumb.jpg", "https://redacted.sh/t/thumb.jpg"),
        ("https://files.catbox.moe/x.jpg?keep=1", "https://files.catbox.moe/x.jpg?keep=1"),
        ("not a url", "not a url"),
    ],
)
def test_bare_image_url_strips_only_red_credentials(url, expected) -> None:
    assert red.bare_image_url(url) == expected


def test_red_refuses_off_origin_image_url(monkeypatch, tmp_path) -> None:
    # The image URL is server-controlled; only a RED-origin URL may become a cover.
    _patch_red_env(monkeypatch, {"status": "success", "response": {"url": "https://evil.example/i/x.png"}})
    image = tmp_path / "image.png"
    image.write_bytes(b"png-data")

    with pytest.raises(ImageUploadFailed, match="evil.example"):
        anyio.run(red.ImageUploader().upload_file, str(image))


def _upload_against(monkeypatch, tmp_path, payload: dict):
    _patch_red_env(monkeypatch, payload)
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"\xff\xd8\xff")
    return anyio.run(red.ImageUploader().upload_file, str(image))


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("Image must be album art", "Image must be album art"),
        ({"code": 7, "text": "quota"}, "quota"),
    ],
)
def test_reds_rejection_reason_reaches_the_user(reason, expected, monkeypatch, tmp_path) -> None:
    with pytest.raises(ImageUploadFailed) as excinfo:
        _upload_against(monkeypatch, tmp_path, {"status": "failure", "error": reason})
    assert expected in str(excinfo.value)


def test_a_rejection_without_a_reason_still_fails_cleanly(monkeypatch, tmp_path) -> None:
    with pytest.raises(ImageUploadFailed):
        _upload_against(monkeypatch, tmp_path, {"status": "failure"})


def test_a_rejection_reason_cannot_leak_credentials(monkeypatch, tmp_path) -> None:
    # RED controls this string; tracker responses embed authkey/torrent_pass in download links.
    reason = (
        "could not fetch https://redacted.sh/torrents.php?action=download"
        "&authkey=SYNTHETIC-AUTHKEY-VALUE&torrent_pass=SYNTHETIC-PASS-VALUE"
    )
    with pytest.raises(ImageUploadFailed) as excinfo:
        _upload_against(monkeypatch, tmp_path, {"status": "failure", "error": reason})
    message = str(excinfo.value)
    assert "SYNTHETIC-AUTHKEY-VALUE" not in message
    assert "SYNTHETIC-PASS-VALUE" not in message
    assert "REDACTED" in message


def test_a_failed_request_closes_the_client_and_cannot_leak_credentials(monkeypatch, tmp_path) -> None:
    _patch_red_env(monkeypatch)

    async def refuse(*_args, **_kwargs):
        raise RequestFailedError("<a href='torrents.php?action=download&authkey=SYNTHETIC-AUTHKEY-VALUE'>")

    monkeypatch.setattr(_FakeRed, "_request", refuse)
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"\xff\xd8\xff")

    with pytest.raises(ImageUploadFailed) as excinfo:
        anyio.run(red.ImageUploader().upload_file, str(image))
    assert "SYNTHETIC-AUTHKEY-VALUE" not in str(excinfo.value)
    assert _FakeRed.made[0].closed
