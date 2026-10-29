"""What this fork's remove_local_after_copy adds to upstream's: a failed conversion keeps the local copies."""

from pathlib import Path
from typing import Any

import pytest
from test_uploader_after_first_upload import _removals, _trackers  # pyright: ignore[reportMissingImports]
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    _album,
    _loose_rate_limit,  # noqa: F401 (a fixture: see pytestmark)
    _run_up,
    _transcode,
    image_uploads,  # noqa: F401 (a fixture: see pytestmark)
)

from salmon import cfg
from salmon.errors import UploadError

pytestmark = pytest.mark.usefixtures("image_uploads", "_loose_rate_limit")


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a dot_torrents_dir, configured."""
    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return downloads, torrents


def test_a_failed_conversion_keeps_the_local_copies(monkeypatch, tmp_path, dirs) -> None:
    _downloads, torrents = dirs
    _trackers(monkeypatch, "RED")
    removals = _removals(monkeypatch)

    async def no_320(path: str, bitrate: str, *args: Any, **kwargs: Any) -> str:
        if bitrate == "320":
            raise UploadError("lame gave up")
        return await _transcode(path, bitrate, *args, **kwargs)

    run = _run_up(monkeypatch, _album(tmp_path / "Album"), torrents, input="\n\n", transcode_folder=no_320)

    assert run.result.exit_code == 0, run.result.output
    assert removals == ["the MP3 320 conversion failed"]
