"""In-torrent path measurement — the thing trackers actually count."""

import os

import pytest

from salmon.checks.tag_rules import STRICTEST_PATH_LENGTH, in_torrent_path
from salmon.errors import NoncompliantFolderStructure
from salmon.tagger.folderstructure import _check_path_lengths

LIMIT = STRICTEST_PATH_LENGTH


def _album(tmp_path, in_torrent_len, folder="Album Folder"):
    """Build an album whose single file's in-torrent path is exactly in_torrent_len."""
    d = tmp_path / folder
    d.mkdir(parents=True)
    stem_len = in_torrent_len - len(folder) - 1 - len(".flac")
    (d / ("x" * stem_len + ".flac")).write_bytes(b"x")
    return str(d)


def _measured(album):
    name = os.path.basename(album)
    return max(
        len(in_torrent_path(name, os.path.relpath(os.path.join(r, f), album)))
        for r, _, fs in os.walk(album)
        for f in fs
    )


def test_in_torrent_path_is_the_folder_plus_what_is_under_it():
    assert in_torrent_path("Album", "01 - track.flac") == "Album/01 - track.flac"
    # os.walk yields "." for the album root; it must not become "Album/."
    assert in_torrent_path("Album", ".") == "Album"
    assert in_torrent_path("Album", "") == "Album"


def test_measurement_does_not_depend_on_where_the_album_sits_on_disk(tmp_path):
    """The old measure subtracted len(download_directory), which only matched when
    the album happened to live there and under-counted everywhere else."""
    shallow = _album(tmp_path / "a", LIMIT + 20)
    deep = _album(tmp_path / "a" / "bb" / "ccc" / "dddd", LIMIT + 20)

    assert _measured(shallow) == _measured(deep) == LIMIT + 20


def test_an_over_limit_path_is_truncated_to_exactly_the_limit(tmp_path):
    album = _album(tmp_path / "a", LIMIT + 20)

    _check_path_lengths(album, scene=False)

    assert _measured(album) == LIMIT, "truncation must land exactly on the limit, not near it"
    assert len(os.listdir(album)) == 1, "the file was renamed, not duplicated or lost"
    assert os.listdir(album)[0].endswith("...flac")


def test_a_path_within_the_limit_is_left_alone(tmp_path):
    album = _album(tmp_path / "a", LIMIT)
    before = os.listdir(album)

    _check_path_lengths(album, scene=False)

    assert os.listdir(album) == before


def test_a_filename_too_short_to_absorb_the_overshoot_raises(tmp_path):
    """Trimming happens in the basename, so a long folder with a short filename
    cannot be fixed by shortening the filename."""
    album = tmp_path / ("A" * 100)
    sub = album / ("B" * 72)
    sub.mkdir(parents=True)
    (sub / "ab.flac").write_bytes(b"x")

    with pytest.raises(NoncompliantFolderStructure):
        _check_path_lengths(str(album), scene=False)


def test_a_scene_release_is_never_auto_truncated(tmp_path):
    album = _album(tmp_path / "a", LIMIT + 20)
    before = os.listdir(album)

    with pytest.raises(NoncompliantFolderStructure):
        _check_path_lengths(album, scene=True)

    assert os.listdir(album) == before, "a scene release must be left for manual descening"
