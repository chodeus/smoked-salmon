"""The MQA gate the upload runs — it must not be weaker than the album check."""

import os

import asyncclick as click
import pytest

import salmon.checks
import salmon.checks.album as album_mod
from salmon import cfg
from salmon.checks import mqa_test


def _album(tmp_path, count=4):
    for i in range(1, count + 1):
        (tmp_path / f"{i:02d} - track.flac").write_bytes(b"x")
    return str(tmp_path)


async def test_mqa_on_a_later_track_still_aborts(monkeypatch, tmp_path):
    """The old gate read only the first file os.walk happened to yield, so MQA on
    any other track shipped. MQA is a hard trump — every file has to be read."""
    path = _album(tmp_path)
    seen: list[str] = []

    async def fake_check_mqa(p):
        seen.append(os.path.basename(p))
        return os.path.basename(p).startswith("04")

    monkeypatch.setattr(album_mod, "check_mqa", fake_check_mqa)

    with pytest.raises(click.Abort):
        await mqa_test(path)

    assert len(seen) == 4, "every file must be read, not just the first"


async def test_a_clean_album_does_not_abort(monkeypatch, tmp_path):
    path = _album(tmp_path)

    async def fake_check_mqa(_p):
        return False

    monkeypatch.setattr(album_mod, "check_mqa", fake_check_mqa)

    await mqa_test(path)  # must not raise


def test_every_file_is_the_default() -> None:
    assert cfg.upload.mqa_check_all_tracks is True


async def test_with_the_opt_out_only_the_first_file_is_read(monkeypatch, tmp_path):
    """upload.mqa_check_all_tracks = false: faster, and MQA on track 4 is not caught."""
    path = _album(tmp_path)
    seen: list[str] = []

    async def fake_check_mqa(p):
        seen.append(os.path.basename(p))
        return os.path.basename(p).startswith("04")

    monkeypatch.setattr(salmon.checks, "check_mqa", fake_check_mqa)
    monkeypatch.setattr(album_mod, "check_mqa", fake_check_mqa)

    await mqa_test(path, all_files=False)

    assert seen == ["01 - track.flac"]


async def test_with_the_opt_out_mqa_on_the_first_file_still_aborts(monkeypatch, tmp_path):
    path = _album(tmp_path)

    async def fake_check_mqa(p):
        return os.path.basename(p).startswith("01")

    monkeypatch.setattr(salmon.checks, "check_mqa", fake_check_mqa)

    with pytest.raises(click.Abort):
        await mqa_test(path, all_files=False)
