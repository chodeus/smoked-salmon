"""A multi-file mutation that fails partway must never continue silently.

None of these can be atomic, so the album really can end up inconsistent. The
requirement is that it is reported and the run stops — continuing would upload a
release whose tracks disagree with each other.
"""

import asyncclick
import pytest

from salmon.tagger.mutation import abort_partial


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
