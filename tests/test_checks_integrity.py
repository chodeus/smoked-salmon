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


def test_a_warning_from_mp3val_is_carried_as_a_concern_not_swallowed(monkeypatch):
    """mp3val exits 0 while describing the damage, so its text is the verdict."""
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(
            returncode=0,
            stdout=b'INFO: "a.mp3": 100 MPEG frames\nWARNING: "a.mp3": It seems that file is truncated\n',
        )

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    result = asyncio.run(ig._check_mp3_integrity("/music/a.mp3"))

    assert result.passed, "a truncated-but-playable file still decodes; it must not block"
    assert len(result.concerns) == 1
    assert "truncated" in result.concerns[0]


def test_an_mp3val_error_fails_the_check(monkeypatch):
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(returncode=0, stdout=b'ERROR: "a.mp3": Unable to open file\n')

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    result = asyncio.run(ig._check_mp3_integrity("/music/a.mp3"))

    assert not result.passed


def test_a_clean_mp3_passes_with_nothing_to_report(monkeypatch):
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(returncode=0, stdout=b'INFO: "a.mp3": 7320 MPEG frames (MPEG 1 Layer III), CBR\n')

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    result = asyncio.run(ig._check_mp3_integrity("/music/a.mp3"))

    assert result.passed
    assert result.concerns == ()
