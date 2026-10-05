"""salmon images up -t: the default host is the tracker's, and a host not allowed there is refused."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import msgspec
import pytest
from asyncclick.testing import CliRunner

import salmon.trackers
from salmon import images
from salmon.config.validations import ImageUploader


class _Fake:
    """An image host that sends nothing anywhere and records what it was asked to upload."""

    def __init__(self, host: str, log: list[str]):
        self.host = host
        self.log = log

    @asynccontextmanager
    async def connections(self, _limit: int) -> AsyncIterator[None]:
        yield

    async def upload_file(self, _filename: str) -> tuple[str, None]:
        self.log.append(self.host)
        return f"https://{self.host}.invalid/x.png", None


@pytest.fixture
def uploads(monkeypatch) -> list[str]:
    """The hosts uploaded to, in order. Every host is a fake, and the configured trackers are RED, OPS and DIC."""
    log: list[str] = []
    for name in list(images.HOSTS):
        monkeypatch.setitem(images.HOSTS, name, SimpleNamespace(ImageUploader=lambda name=name: _Fake(name, log)))
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])
    monkeypatch.setattr(images.cfg.upload.description, "copy_uploaded_url_to_clipboard", False)
    return log


def _config(monkeypatch, **settings: Any) -> None:
    monkeypatch.setattr(images.cfg, "image", msgspec.convert(settings, ImageUploader))


def _up(tmp_path: Path, *args: str):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")

    async def run():
        return await CliRunner().invoke(images.up, [str(image), *args])

    return anyio.run(run)


def test_tracker_uses_its_own_image_uploader(monkeypatch, tmp_path, uploads) -> None:
    _config(monkeypatch, image_uploader="catbox", ops={"image_uploader": "imgbox"})
    result = _up(tmp_path, "-t", "OPS")
    assert result.exit_code == 0, result.output
    assert uploads == ["imgbox"]


def test_tracker_without_its_own_setting_falls_back_to_the_shared_one(monkeypatch, tmp_path, uploads) -> None:
    _config(monkeypatch, image_uploader="imgbb", imgbb_key="k", red={"image_uploader": "catbox"})
    result = _up(tmp_path, "-t", "ops")
    assert result.exit_code == 0, result.output
    assert uploads == ["imgbb"]


def test_explicit_host_wins_over_the_trackers(monkeypatch, tmp_path, uploads) -> None:
    _config(monkeypatch, image_uploader="imgbox", ops={"image_uploader": "imgbox"})
    result = _up(tmp_path, "-i", "catbox", "-t", "OPS")
    assert result.exit_code == 0, result.output
    assert uploads == ["catbox"]


@pytest.mark.parametrize("tracker", ["DIC", "OPS"])
def test_red_host_is_refused_for_another_tracker_and_nothing_is_uploaded(
    monkeypatch, tmp_path, uploads, tracker
) -> None:
    # OPS gets RED-hosted images only from a cross-upload or a run uploading to both, never by choice.
    _config(monkeypatch)
    result = _up(tmp_path, "-i", "red", "-t", tracker)
    assert result.exit_code == 2
    assert f"red can't be used for {tracker}'s images: RED's image host is for album artwork only" in result.output
    assert uploads == []


def test_red_host_is_accepted_for_red(monkeypatch, tmp_path, uploads) -> None:
    _config(monkeypatch)
    result = _up(tmp_path, "-i", "red", "-t", "RED")
    assert result.exit_code == 0, result.output
    assert uploads == ["red"]


def test_ra_is_accepted_for_any_tracker(monkeypatch, tmp_path, uploads) -> None:
    _config(monkeypatch)
    result = _up(tmp_path, "-i", "ra", "-t", "DIC")
    assert result.exit_code == 0, result.output
    assert uploads == ["ra"]


@pytest.mark.parametrize("tracker", ["XYZ", "ops"])
def test_an_unknown_or_unconfigured_tracker_is_a_usage_error(monkeypatch, tmp_path, uploads, tracker) -> None:
    _config(monkeypatch)
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    result = _up(tmp_path, "-t", tracker)
    assert result.exit_code == 2
    assert "is not a tracker in your config (configured: RED)" in result.output
    assert uploads == []


def test_without_a_tracker_nothing_changes(monkeypatch, tmp_path, uploads) -> None:
    # Regression guard: [image.<tracker>] settings are not read, the default is
    # [image] image_uploader, and -i is accepted as is, red included (there is no tracker to refuse it for).
    _config(monkeypatch, image_uploader="imgbox", ops={"image_uploader": "catbox"})
    default_result = _up(tmp_path)
    explicit_result = _up(tmp_path, "-i", "red")
    assert default_result.exit_code == 0, default_result.output
    assert explicit_result.exit_code == 0, explicit_result.output
    assert uploads == ["imgbox", "red"]
    result = _up(tmp_path, "-i", "nope")
    assert result.exit_code == 2
    assert "nope is not a valid image host" in result.output


def test_the_resolution_serves_other_callers(monkeypatch) -> None:
    _config(monkeypatch, image_uploader="imgbox", dic={"image_uploader": "catbox"})
    assert images.image_host_for_tracker("DIC") == "catbox"
    assert images.image_host_for_tracker("RED") == "imgbox"
    assert images.image_host_for_tracker("RED", "red") == "red"
    for tracker in ("OPS", "DIC"):
        with pytest.raises(images.ImageHostRefused, match="album artwork only"):
            images.image_host_for_tracker(tracker, "red")
