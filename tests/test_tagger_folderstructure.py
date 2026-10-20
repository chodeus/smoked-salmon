"""Path-length truncation safety in salmon.tagger.folderstructure."""

from salmon import cfg
from salmon.tagger.folderstructure import _check_path_lengths


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
