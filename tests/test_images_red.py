import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import msgspec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
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


class _Session:
    def __init__(self, number: int, **kwargs):
        self.number = number
        self.kwargs = kwargs
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, url: str, *, params: dict, data):
        self.calls.append(("post", url, params))
        return _Response({"status": "success", "response": {"url": f"https://redacted.sh/i/image-{self.number}.png"}})

    def get(self, url: str, *, params: dict):
        self.calls.append(("get", url, params))
        return _Response(
            {
                "status": "success",
                "response": {"h": f"key-{self.number}", "e": 123456 + self.number, "u": self.number},
            }
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
    monkeypatch.setattr(red.ImageUploader, "_image_auth", None)
    monkeypatch.setattr(red.ImageUploader, "_image_auth_expires_at", 0.0)
    monkeypatch.setattr(red.ImageUploader, "_image_auth_session", None)
    current_time = 100.0
    monkeypatch.setattr(red.time, "time", lambda: current_time)
    monkeypatch.setattr(
        red,
        "cfg",
        SimpleNamespace(
            tracker=SimpleNamespace(red=SimpleNamespace(session="red-session")),
            upload=SimpleNamespace(user_agent="salmon-test"),
        ),
    )
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
        ("post", red.AJAX_URL, {"action": "upload_image"}),
        ("get", red.AJAX_URL, {"action": "imgauth"}),
    ]
    assert sessions[0].calls == expected_calls
    assert sessions[1].calls == expected_calls[:1]
    assert sessions[2].calls == expected_calls
    assert all(session.kwargs["cookies"] == {"session": "red-session"} for session in sessions)
