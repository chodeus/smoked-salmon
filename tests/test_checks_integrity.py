"""Backup-path reservation in salmon.checks.integrity."""

from salmon.checks.integrity import _reserve_backup_path


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
