"""Backup-path reservation in salmon.checks.integrity."""

from salmon.checks.integrity import _reserve_backup_path


def test_reserve_backup_path_avoids_stale_backups(tmp_path) -> None:
    f = tmp_path / "a.flac"
    f.write_text("x")
    assert _reserve_backup_path(str(f)) == f"{f}.corrupted"

    (tmp_path / "a.flac.corrupted").write_text("stale")
    assert _reserve_backup_path(str(f)) == f"{f}.corrupted.1"

    (tmp_path / "a.flac.corrupted.1").write_text("stale2")
    assert _reserve_backup_path(str(f)) == f"{f}.corrupted.2"
