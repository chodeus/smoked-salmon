"""What this fork's link handling adds to upstream's: a vanished file, and no rename over a folder sharing files."""

import os
from pathlib import Path

import anyio
import pytest
from test_library_dirs import RENAMED, _album, _metadata, _returning  # pyright: ignore[reportMissingImports]
from test_shared_files import _share  # pyright: ignore[reportMissingImports]

import salmon.tagger.foldername
from salmon import cfg
from salmon.common.files import shares_files
from salmon.errors import UploadError


@pytest.fixture
def seeded(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory, and the folder a torrent client seeds from beside it; no library_dirs."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    seeding = tmp_path / "seeding"
    seeding.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    return downloads, seeding


def test_a_file_gone_between_the_two_checks_counts_as_sharing_files(monkeypatch, seeded) -> None:
    downloads, _seeding = seeded
    gone = _album(downloads / "Album") / "gone.flac"
    # Still a file when isfile looked; gone when its links are counted.
    monkeypatch.setattr(os.path, "isfile", lambda path: os.fspath(path) == str(gone))

    assert shares_files(str(gone))


def test_rename_folder_never_replaces_an_earlier_folder_of_that_name_that_shares_files(monkeypatch, seeded) -> None:
    # An earlier run's renamed folder shares its files with that run's source when hardlinks is on.
    downloads, seeding = seeded
    earlier = _album(downloads / RENAMED)
    _share(earlier, seeding, "hardlinked file")
    source = _album(seeding / "Other")
    monkeypatch.setattr(salmon.tagger.foldername, "generate_folder_name", _returning(RENAMED))
    monkeypatch.setattr(cfg.directory, "hardlinks", False)

    with pytest.raises(UploadError, match="it shares files with another folder"):
        anyio.run(
            lambda: salmon.tagger.foldername.rename_folder(str(source), _metadata(), auto_rename=True, check=False)
        )

    assert os.path.isdir(earlier)
    assert os.path.isdir(source)
