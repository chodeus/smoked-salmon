import anyio
import msgspec
import pytest

import salmon.uploader
from salmon.config.validations import ImageUploader


@pytest.fixture(autouse=True)
def _interactive(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", False)


def _uploads(monkeypatch, *urls: str | None) -> list[str | None]:
    """Make upload_cover return urls in turn. Returns the host of each call."""
    hosts: list[str | None] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        return "cover.jpg", False

    async def fake_upload(cover_path: str | None, site_code: str | None = None) -> str | None:
        hosts.append(salmon.uploader.cfg.image.resolve(site_code, "cover_uploader"))
        return urls[len(hosts) - 1]

    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)
    monkeypatch.setattr(salmon.uploader, "upload_cover", fake_upload)
    return hosts


def _answers(monkeypatch, *answers: str) -> list[str]:
    """Answer the prompts with answers in turn. Returns the prompts asked."""
    asked: list[str] = []

    async def fake_prompt(text: str, *_args, **_kwargs) -> str:
        asked.append(text)
        return answers[len(asked) - 1]

    monkeypatch.setattr(salmon.uploader.click, "prompt", fake_prompt)
    return asked


def _resolve(group_id: int | None = None) -> tuple[bool, str | None]:
    return anyio.run(salmon.uploader.resolve_cover_url, "RED", group_id, {}, "/release", None, False)


def _use_hosts(monkeypatch, config: dict) -> None:
    monkeypatch.setattr(salmon.uploader.cfg, "image", msgspec.convert(config, ImageUploader))


def test_yes_all_does_not_upload_a_new_group_without_a_cover(monkeypatch) -> None:
    monkeypatch.setattr(salmon.uploader.cfg.upload, "yes_all", True)
    _uploads(monkeypatch, None)
    asked = _answers(monkeypatch)

    assert _resolve() == (False, None)
    assert asked == []


@pytest.mark.parametrize("answer", ["n", "", "no", "x"])
def test_declining_to_go_on_without_a_cover_stops(monkeypatch, answer: str) -> None:
    _uploads(monkeypatch, None)
    asked = _answers(monkeypatch, answer)

    assert _resolve() == (False, None)
    assert len(asked) == 1


@pytest.mark.parametrize("answer", ["y", "Yes "])
def test_accepting_to_go_on_without_a_cover_uploads_without_one(monkeypatch, answer: str) -> None:
    _uploads(monkeypatch, None)
    _answers(monkeypatch, answer)

    assert _resolve() == (True, None)


@pytest.mark.parametrize("answer", ["r", "reload"])
def test_reload_uses_a_cover_that_appeared(monkeypatch, answer: str) -> None:
    hosts = _uploads(monkeypatch, None, None, "https://host/cover.jpg")
    asked = _answers(monkeypatch, answer, answer)

    assert _resolve() == (True, "https://host/cover.jpg")
    assert len(hosts) == 3
    assert len(asked) == 2


def test_existing_group_needs_no_cover(monkeypatch) -> None:
    hosts = _uploads(monkeypatch)
    asked = _answers(monkeypatch)
    downloads: list[str | None] = []

    async def fake_download(path: str, cover_source: str | None) -> tuple[str, bool]:
        downloads.append(cover_source)
        return "cover.jpg", True

    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", fake_download)

    assert _resolve(group_id=123) == (True, None)
    assert hosts == []
    assert asked == []
    assert downloads == [None]


def test_available_cover_needs_no_prompt(monkeypatch) -> None:
    hosts = _uploads(monkeypatch, "https://host/cover.jpg")
    asked = _answers(monkeypatch)

    assert _resolve() == (True, "https://host/cover.jpg")
    assert len(hosts) == 1
    assert asked == []


def test_reload_does_not_upload_again_to_a_host_that_has_the_cover(monkeypatch) -> None:
    _use_hosts(monkeypatch, {"cover_uploader": "imgbox", "red": {"cover_uploader": "red"}})
    hosts = _uploads(monkeypatch, "https://imgbox/cover.jpg", None, "https://red/i/cover.jpg")
    _answers(monkeypatch, "r")

    async def run() -> list[tuple[bool, str | None]]:
        stored: dict[str, str] = {}
        return [
            await salmon.uploader.resolve_cover_url(tracker, None, stored, "/release", None, False)
            for tracker in ("DIC", "RED")
        ]

    result = anyio.run(run)
    assert result == [(True, "https://imgbox/cover.jpg"), (True, "https://red/i/cover.jpg")]
    assert hosts == ["imgbox", "red", "red"]


def test_ops_reuses_the_red_cover_without_uploading(monkeypatch) -> None:
    _use_hosts(monkeypatch, {"cover_uploader": "imgbox", "red": {"cover_uploader": "red"}})
    hosts = _uploads(monkeypatch, "https://red/i/cover.jpg")
    asked = _answers(monkeypatch)

    async def run() -> list[tuple[bool, str | None]]:
        stored: dict[str, str] = {}
        return [
            await salmon.uploader.resolve_cover_url(tracker, None, stored, "/release", None, False)
            for tracker in ("RED", "OPS")
        ]

    result = anyio.run(run)
    assert result == [(True, "https://red/i/cover.jpg"), (True, "https://red/i/cover.jpg")]
    assert hosts == ["red"]
    assert asked == []
