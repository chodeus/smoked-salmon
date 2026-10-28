"""An album that shares files through a hardlink or symlink is never rewritten in place: what it shares stays as it is.

The case: a release a torrent client seeds, hardlinked (or symlinked) into another folder that salmon is run on. A tag
write goes into the shared inode, or through the symlink, so retagging that folder in place would change the seeded
files and fail the torrent's recheck. No library_dirs is configured here: the rule holds without one.
"""

import os
from pathlib import Path
from typing import Any

import pytest
from test_library_dirs import (  # pyright: ignore[reportMissingImports]
    _album,
)

from salmon import cfg
from salmon.common.files import rewrite_refusal, shares_files

SHARING = ["hardlinked file", "symlinked file", "symlinked folder"]


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


def _share(album: Path, seeding: Path, how: str) -> Path:
    """Share part of album with a seeded copy of it, the way `how` says; return the seeded folder."""
    seeded = seeding / "Album"
    seeded.mkdir()
    try:
        if how == "hardlinked file":
            os.link(album / "01 - one.flac", seeded / "01 - one.flac")
        elif how == "symlinked file":
            (album / "01 - one.flac").rename(seeded / "01 - one.flac")
            (album / "01 - one.flac").symlink_to(seeded / "01 - one.flac")
        else:
            (album / "CD1").rename(seeded / "CD1")
            (album / "CD1").symlink_to(seeded / "CD1", target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"this filesystem refuses the link: {error}")
    return seeded


def _symlinked_album(downloads: Path, seeding: Path) -> Path:
    """An album folder that is itself a symlink to the seeded folder; return the link."""
    seeded = _album(seeding / "Album")
    link = downloads / "Album"
    try:
        link.symlink_to(seeded, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"this filesystem refuses symlinks: {error}")
    return link


# shares_files and rewrite_refusal


@pytest.mark.parametrize("how", SHARING)
def test_an_album_sharing_a_file_or_folder_shares_files(seeded, how: str) -> None:
    downloads, seeding = seeded
    album = _album(downloads / "Album")
    _share(album, seeding, how)

    assert shares_files(str(album))
    assert rewrite_refusal(str(album)) == "it shares files with another folder through a hardlink or symlink"


def test_an_album_that_is_a_symlink_shares_files(seeded) -> None:
    link = _symlinked_album(*seeded)

    assert shares_files(str(link))
    assert not shares_files(os.path.realpath(link))


def test_an_album_with_no_link_shares_nothing(seeded) -> None:
    downloads, _seeding = seeded
    album = _album(downloads / "Album")

    assert not shares_files(str(album))
    assert rewrite_refusal(str(album)) is None


def test_a_file_with_a_second_hardlink_shares_files(seeded) -> None:
    downloads, seeding = seeded
    album = _album(downloads / "Album")
    _share(album, seeding, "hardlinked file")

    assert shares_files(str(album / "01 - one.flac"))
    assert not shares_files(str(album / "CD1" / "02 - two.flac"))


def test_an_unreadable_folder_counts_as_sharing_files(seeded) -> None:
    downloads, _seeding = seeded
    album = _album(downloads / "Album")
    (album / "CD1").chmod(0)
    try:
        if os.access(album / "CD1", os.R_OK):
            pytest.skip("permission bits are not enforced for this user")
        assert shares_files(str(album))
    finally:
        (album / "CD1").chmod(0o755)


def test_an_entry_that_vanishes_mid_walk_counts_as_sharing_files(monkeypatch, seeded) -> None:
    downloads, _seeding = seeded
    album = _album(downloads / "Album")
    real_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(path).endswith("cover.jpg"):
            raise FileNotFoundError(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)

    assert shares_files(str(album))
