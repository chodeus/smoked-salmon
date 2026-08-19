"""Backup-path reservation and restore behavior in salmon.checks.integrity."""

import anyio
import pytest

from salmon.checks.integrity import _reserve_backup_path, _sanitize_flac


def test_reserve_backup_path_claims_atomically_and_skips_stale(tmp_path) -> None:
    f = tmp_path / "a.flac"
    f.write_text("x")

    first = _reserve_backup_path(str(f))
    assert first == f"{f}.corrupted"
    # The claim creates a placeholder, so a second claimer can never pick the
    # same name (O_EXCL) — this is the concurrency guarantee.
    assert (tmp_path / "a.flac.corrupted").exists()

    second = _reserve_backup_path(str(f))
    assert second == f"{f}.corrupted.1"
    assert (tmp_path / "a.flac.corrupted.1").exists()

    third = _reserve_backup_path(str(f))
    assert third == f"{f}.corrupted.2"


async def test_sanitize_flac_restores_original_on_cancellation(tmp_path, monkeypatch) -> None:
    # Cancellation is not an Exception; without the BaseException handler the
    # file would stay renamed aside as .corrupted.
    f = tmp_path / "a.flac"
    f.write_text("DATA")

    async def cancelled(*_args, **_kwargs):
        raise anyio.get_cancelled_exc_class()()

    monkeypatch.setattr(anyio, "run_process", cancelled)

    with pytest.raises(anyio.get_cancelled_exc_class()):
        await _sanitize_flac(str(f))

    assert f.read_text() == "DATA"
    assert not list(tmp_path.glob("*.corrupted*"))
