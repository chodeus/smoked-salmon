"""A multi-file mutation that fails partway must never continue silently.

None of these can be atomic, so the album really can end up inconsistent. The
requirement is that it is reported and the run stops — continuing would upload a
release whose tracks disagree with each other.
"""

import os

import asyncclick
import pytest
from mutagen.flac import FLAC
from test_tagger_tags import _write_flac  # pyright: ignore[reportMissingImports]

from salmon.tagger import retagger, tags
from salmon.tagger.mutation import abort_partial
from salmon.tagger.retagger import Change


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


# Through the album-wide writes


def _fails_to_save(opened, name: str):
    """`opened`, with the file named `name` failing to save, as on a full disk."""

    def opening(path, *args, **kwargs):
        mut = opened(path, *args, **kwargs)
        if os.path.basename(path) == name:

            def full_disk(*_args, **_kwargs) -> None:
                raise OSError(28, "No space left on device")

            mut.save = full_disk
        return mut

    return opening


def test_a_retag_that_fails_partway_names_what_it_changed_and_stops(tmp_path, monkeypatch, capsys) -> None:
    for name in ("01.flac", "02.flac", "03.flac"):
        _write_flac(tmp_path / name, title="Old")
    monkeypatch.setattr(retagger, "TagFile", _fails_to_save(retagger.TagFile, "02.flac"))
    changes = {name: [Change("title", "Old", "New")] for name in ("01.flac", "02.flac", "03.flac")}

    with pytest.raises(asyncclick.Abort):
        retagger.retag_files(str(tmp_path), {}, changes)

    out = capsys.readouterr().out
    assert "Already changed: 01.flac" in out
    assert "May be partially changed: 02.flac" in out
    assert "Not changed: 03.flac" in out
    assert [FLAC(tmp_path / name)["title"] for name in ("01.flac", "03.flac")] == [["New"], ["Old"]]


def test_standardizing_tags_that_fails_partway_names_what_it_changed_and_stops(tmp_path, monkeypatch, capsys) -> None:
    for name in ("01.flac", "02.flac", "03.flac"):
        _write_flac(tmp_path / name, year="2001")
    monkeypatch.setattr(tags, "MutagenFile", _fails_to_save(tags.MutagenFile, "02.flac"))

    with pytest.raises(asyncclick.Abort):
        tags.standardize_tags(str(tmp_path))

    out = capsys.readouterr().out
    assert "Already changed: 01.flac" in out
    assert "May be partially changed: 02.flac" in out
    assert "Not changed: 03.flac" in out
    assert "date" in FLAC(tmp_path / "01.flac") and "year" in FLAC(tmp_path / "03.flac")
