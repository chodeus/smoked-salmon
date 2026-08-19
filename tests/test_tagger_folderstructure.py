"""Path-length truncation safety in salmon.tagger.folderstructure."""

import pytest

from salmon import cfg
from salmon.errors import NoncompliantFolderStructure
from salmon.tagger.folderstructure import _check_path_lengths


def test_truncation_refuses_when_targets_collide(tmp_path, monkeypatch) -> None:
    # Two names differing only past the truncation cut would rename to the SAME
    # target; os.rename would destroy one — the check must refuse instead.
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "Album"
    album.mkdir()
    stem = "x" * 200
    a = album / f"{stem}A.flac"
    b = album / f"{stem}B.flac"
    a.write_text("A")
    b.write_text("B")

    with pytest.raises(NoncompliantFolderStructure):
        _check_path_lengths(str(album), False)

    assert a.read_text() == "A"
    assert b.read_text() == "B"


def test_truncation_still_renames_distinct_names(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "Album"
    album.mkdir()
    long_file = album / ("y" * 200 + ".flac")
    long_file.write_text("Y")

    _check_path_lengths(str(album), False)

    assert not long_file.exists()
    truncated = list(album.glob("*.flac"))
    assert len(truncated) == 1
    assert truncated[0].read_text() == "Y"
