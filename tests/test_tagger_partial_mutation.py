"""A multi-file mutation that fails partway must never continue silently.

None of these can be atomic, so the album really can end up inconsistent. The
requirement is that it is reported and the run stops — continuing would upload a
release whose tracks disagree with each other.
"""

import errno
import os

import asyncclick
import pytest

from salmon.tagger.mutation import abort_partial, rename_all_or_none


def test_abort_partial_names_what_changed_and_stops(capsys):
    with pytest.raises(asyncclick.Abort):
        abort_partial("Retagging 02.flac", ["01.flac"], "02.flac", ["03.flac"], OSError("disk full"))
    out = capsys.readouterr().out
    assert "Already changed: 01.flac" in out
    assert "May be partially changed: 02.flac" in out
    assert "Not changed: 03.flac" in out
    assert "inconsistent" in out


def test_the_failing_file_is_not_called_unchanged(capsys):
    """A write can fail after modifying the file, so its state is unknown."""
    with pytest.raises(asyncclick.Abort):
        abort_partial("Retagging 01.flac", [], "01.flac", [], OSError("denied"))
    out = capsys.readouterr().out
    assert "May be partially changed: 01.flac" in out
    assert "Not changed" not in out
    assert "inconsistent" in out, "a half-written first file is still an inconsistency"


def test_abort_partial_is_quiet_about_consistency_when_nothing_was_touched(capsys):
    with pytest.raises(asyncclick.Abort):
        abort_partial("Reading 01.flac", [], None, ["01.flac"], OSError("denied"))
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
    import salmon.tagger.mutation as m

    for n in ("a.flac", "b.flac"):
        (tmp_path / n).write_text(n)
    renames = [(str(tmp_path / n), str(tmp_path / f"new-{n}")) for n in ("a.flac", "b.flac")]
    (tmp_path / "new-b.flac").write_text("blocks the second rename")

    real = m._rename_no_clobber

    def flaky(src, dst):
        if os.path.basename(dst) == "a.flac":
            raise OSError(13, "Permission denied")  # the undo itself fails
        real(src, dst)

    monkeypatch.setattr(m, "_rename_no_clobber", flaky)
    with pytest.raises(asyncclick.Abort):
        m.rename_all_or_none(renames)

    out = capsys.readouterr().out
    assert "Could not restore" in out
    assert "rollback was incomplete" in out
    assert "and was rolled back" not in out


def test_rename_never_overwrites_an_existing_target(tmp_path, capsys):
    """os.rename replaces on POSIX; a target created after the collision check
    would be destroyed and no rollback could bring it back."""
    (tmp_path / "a.flac").write_text("source")
    (tmp_path / "new-a.flac").write_text("bystander")

    with pytest.raises(asyncclick.Abort):
        rename_all_or_none([(str(tmp_path / "a.flac"), str(tmp_path / "new-a.flac"))])

    assert (tmp_path / "new-a.flac").read_text() == "bystander", "the target was overwritten"
    assert (tmp_path / "a.flac").read_text() == "source"


def test_a_source_replaced_mid_rename_is_not_deleted(tmp_path, monkeypatch):
    """Between link and unlink another process can move the original away and put
    a different file at the source path. Unlinking would destroy theirs."""
    import salmon.tagger.mutation as m

    src = tmp_path / "a.flac"
    src.write_text("original")
    real_link = os.link

    def link_then_swap(a, b):
        real_link(a, b)
        os.rename(a, tmp_path / "moved-away.flac")  # concurrent actor
        (tmp_path / "a.flac").write_text("someone else's file")

    monkeypatch.setattr(os, "link", link_then_swap)
    with pytest.raises(OSError, match="source changed mid-rename"):
        m._rename_no_clobber(str(src), str(tmp_path / "new-a.flac"))

    assert (tmp_path / "a.flac").read_text() == "someone else's file", "the replacement was deleted"
    assert (tmp_path / "new-a.flac").read_text() == "original"


def test_a_filesystem_without_hardlinks_aborts_rather_than_racing(tmp_path, monkeypatch):
    """The old fallback was check-then-rename — the very race being removed."""
    import salmon.tagger.mutation as m

    src = tmp_path / "a.flac"
    src.write_text("original")

    def no_links(_a, _b):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    with pytest.raises(OSError, match="refusing rather than risking an overwrite"):
        m._rename_no_clobber(str(src), str(tmp_path / "new-a.flac"))

    assert src.read_text() == "original", "the source must be left alone"
    assert not (tmp_path / "new-a.flac").exists()
