"""A multi-file mutation that fails partway must never continue silently.

None of these can be atomic, so the album really can end up inconsistent. The
requirement is that it is reported and the run stops — continuing would upload a
release whose tracks disagree with each other.
"""

import os

import asyncclick
import pytest

from salmon.tagger.mutation import abort_partial, rename_all_or_none


def test_abort_partial_names_what_changed_and_stops(capsys):
    with pytest.raises(asyncclick.Abort):
        abort_partial("Retagging 02.flac", ["01.flac"], ["02.flac", "03.flac"], OSError("disk full"))
    out = capsys.readouterr().out
    assert "Already changed: 01.flac" in out
    assert "Not changed: 02.flac, 03.flac" in out
    assert "inconsistent" in out


def test_abort_partial_does_not_cry_inconsistent_when_nothing_changed(capsys):
    with pytest.raises(asyncclick.Abort):
        abort_partial("Retagging 01.flac", [], ["01.flac"], OSError("denied"))
    assert "inconsistent" not in capsys.readouterr().out


def test_rename_all_or_none_puts_every_file_back(tmp_path, capsys):
    names = ["a.flac", "b.flac", "c.flac"]
    for n in names:
        (tmp_path / n).write_text(n)
    renames = [(str(tmp_path / n), str(tmp_path / f"new-{n}")) for n in names]
    # c cannot be renamed: its target is a directory that already exists
    (tmp_path / "new-c.flac").mkdir()

    with pytest.raises(asyncclick.Abort):
        rename_all_or_none(renames)

    for n in names:
        assert (tmp_path / n).is_file(), f"{n} was not restored"
        assert (tmp_path / n).read_text() == n
    assert not (tmp_path / "new-a.flac").exists()
    assert "rolled back" in capsys.readouterr().out


def test_rename_all_or_none_completes_when_nothing_fails(tmp_path):
    for n in ("a.flac", "b.flac"):
        (tmp_path / n).write_text(n)
    rename_all_or_none([(str(tmp_path / n), str(tmp_path / f"new-{n}")) for n in ("a.flac", "b.flac")])
    assert sorted(os.listdir(tmp_path)) == ["new-a.flac", "new-b.flac"]


def test_a_failed_restore_is_not_reported_as_a_clean_rollback(tmp_path, monkeypatch, capsys):
    """Saying 'rolled back' after a restore failed is the opposite of true."""
    for n in ("a.flac", "b.flac"):
        (tmp_path / n).write_text(n)
    renames = [(str(tmp_path / n), str(tmp_path / f"new-{n}")) for n in ("a.flac", "b.flac")]
    (tmp_path / "new-b.flac").mkdir()

    real_rename = os.rename

    def rename(src, dst):
        if str(dst).endswith("a.flac") and "new-" not in os.path.basename(str(dst)):
            raise OSError(13, "Permission denied")  # the undo fails
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(asyncclick.Abort):
        rename_all_or_none(renames)

    out = capsys.readouterr().out
    assert "Could not restore" in out
    assert "rollback was incomplete" in out or "the rollback was incomplete" in out
    assert "and was rolled back" not in out
