import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import msgspec
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.errors import ImageUploadFailed
from salmon.images import HOSTS, red
from salmon.images.base import BaseImageUploader


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self) -> None:
        pass

    async def text(self) -> str:
        return msgspec.json.encode(self.payload).decode()


class _CookieJar:
    def __init__(self):
        self.seeded: list[tuple[dict, str]] = []

    def update_cookies(self, cookies, response_url=None):
        self.seeded.append((dict(cookies), str(response_url)))


class _Session:
    def __init__(self, number: int, **kwargs):
        self.number = number
        self.kwargs = kwargs
        self.cookie_jar = _CookieJar()
        self.calls: list[tuple[str, str, dict]] = []
        self.redirect_flags: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, url: str, *, params: dict, data, allow_redirects=True):
        self.calls.append(("post", url, params))
        self.redirect_flags.append(allow_redirects)
        return _Response({"status": "success", "response": {"url": f"https://redacted.sh/i/image-{self.number}.png"}})

    def get(self, url: str, *, params: dict, allow_redirects=True):
        self.calls.append(("get", url, params))
        self.redirect_flags.append(allow_redirects)
        if params["action"] == "index":
            return _Response({"status": "success", "response": {"authkey": "account-authkey"}})
        return _Response(
            {
                "status": "success",
                "response": {"h": f"key-{self.number}", "e": 123456 + self.number, "u": self.number},
            }
        )


def _patch_red_env(monkeypatch):
    """Reset RED uploader class caches and stub config/time for a test."""
    monkeypatch.setattr(red.ImageUploader, "_image_auth", None)
    monkeypatch.setattr(red.ImageUploader, "_image_auth_expires_at", 0.0)
    monkeypatch.setattr(red.ImageUploader, "_image_auth_session", None)
    monkeypatch.setattr(red.ImageUploader, "_authkey", None)
    monkeypatch.setattr(red.ImageUploader, "_authkey_session", None)
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


def test_red_reuses_image_auth_until_expired(monkeypatch, tmp_path) -> None:
    sessions: list[_Session] = []

    def session_factory(**kwargs):
        session = _Session(len(sessions) + 1, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(red.aiohttp, "ClientSession", session_factory)
    _patch_red_env(monkeypatch)
    current_time = 100.0
    monkeypatch.setattr(red.time, "time", lambda: current_time)
    image = tmp_path / "image.png"
    image.write_bytes(b"png-data")

    async def upload_three_times() -> tuple[tuple[str, None], tuple[str, None], tuple[str, None]]:
        first = await red.ImageUploader().upload_file(str(image))
        second = await red.ImageUploader().upload_file(str(image))
        nonlocal current_time
        current_time = 123458.0
        third = await red.ImageUploader().upload_file(str(image))
        return first, second, third

    first, second, third = anyio.run(upload_three_times)

    assert first == ("https://redacted.sh/i/image-1.png?h=key-1&e=123457&u=1", None)
    assert second == ("https://redacted.sh/i/image-2.png?h=key-1&e=123457&u=1", None)
    assert third == ("https://redacted.sh/i/image-3.png?h=key-3&e=123459&u=3", None)
    expected_calls = [
        ("get", red.AJAX_URL, {"action": "index"}),
        ("post", red.AJAX_URL, {"action": "upload_image"}),
        ("get", red.AJAX_URL, {"action": "imgauth"}),
    ]
    assert sessions[0].calls == expected_calls
    assert sessions[1].calls == expected_calls[1:2]
    assert sessions[2].calls == expected_calls[1:]
    # Cookies are jar-scoped to RED (not session-wide) so redirects can't leak them.
    assert all("cookies" not in session.kwargs for session in sessions)
    assert all(
        session.cookie_jar.seeded == [({"session": "red-session"}, red.BASE_URL)] for session in sessions
    )
    # Nothing on the RED ajax endpoints legitimately redirects.
    assert all(flag is False for session in sessions for flag in session.redirect_flags)


def test_red_refuses_off_origin_image_url(monkeypatch, tmp_path) -> None:
    # The image URL is server-controlled; h/e/u credentials must never be
    # appended to a URL on any origin but RED's.
    class _OffOriginSession(_Session):
        def post(self, url: str, *, params: dict, data, allow_redirects=True):
            return _Response({"status": "success", "response": {"url": "https://evil.example/i/x.png"}})

    monkeypatch.setattr(red.aiohttp, "ClientSession", lambda **kwargs: _OffOriginSession(1, **kwargs))
    _patch_red_env(monkeypatch)
    image = tmp_path / "image.png"
    image.write_bytes(b"png-data")

    with pytest.raises(ImageUploadFailed, match="off-origin"):
        anyio.run(red.ImageUploader().upload_file, str(image))
