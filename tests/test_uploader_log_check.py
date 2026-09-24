import anyio
import asyncclick as click
import pytest

import salmon.uploader as uploader


def _album_with_a_log(tmp_path) -> str:
    (tmp_path / "rip.log").write_text("log")
    return str(tmp_path)


def _check_log_raising(error: Exception):
    async def check_log_cambia(_logpath: str, _basepath: str) -> None:
        raise error

    return check_log_cambia


def test_unreadable_audio_aborts_the_upload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(uploader, "check_log_cambia", _check_log_raising(PermissionError(13, "Permission denied")))
    with pytest.raises(click.Abort):
        anyio.run(uploader._check_logs, _album_with_a_log(tmp_path))


def test_other_log_check_errors_are_reported_not_fatal(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(uploader, "check_log_cambia", _check_log_raising(ValueError("No audio files found!")))
    anyio.run(uploader._check_logs, _album_with_a_log(tmp_path))
    out = capsys.readouterr().out
    assert "Error checking log: No audio files found!" in out
