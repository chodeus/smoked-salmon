"""library_dirs: browsable/uploadable sources that must never be deleted."""

import pytest

from salmon import cfg
from salmon.config.validations import Directory
from salmon.webui.validation import allowed_roots, is_within_roots


def test_is_library_path_matches_contents_and_root(tmp_path, monkeypatch) -> None:
    lib = tmp_path / "music"
    (lib / "Artist" / "Album").mkdir(parents=True)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    assert cfg.directory.is_library_path(str(lib))
    assert cfg.directory.is_library_path(str(lib / "Artist" / "Album"))


def test_is_library_path_rejects_sibling_with_shared_prefix(tmp_path, monkeypatch) -> None:
    # "/data/music-old" must not count as inside "/data/music".
    lib = tmp_path / "music"
    lib.mkdir()
    sibling = tmp_path / "music-old"
    sibling.mkdir()
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    assert not cfg.directory.is_library_path(str(sibling))


def test_no_library_dirs_means_nothing_is_protected(monkeypatch) -> None:
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    assert not cfg.directory.is_library_path("/anywhere")


def test_library_dirs_become_browsable_roots(tmp_path, monkeypatch) -> None:
    lib = tmp_path / "music"
    (lib / "Artist").mkdir(parents=True)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    assert str(lib.resolve()) in allowed_roots()
    assert is_within_roots(str((lib / "Artist").resolve()))


def test_invalid_library_dir_fails_at_config_load(tmp_path) -> None:
    with pytest.raises(ValueError, match="library_dirs"):
        Directory(
            dottorrents_dir=str(tmp_path),
            download_directory=str(tmp_path),
            library_dirs=[str(tmp_path / "does-not-exist")],
        )


def test_writable_validator_rejects_library_but_allows_staging(tmp_path, monkeypatch) -> None:
    # Transcode/downconvert write a sibling folder and spectrals write into the
    # album, so neither may target a read-only library source.
    from fastapi import HTTPException

    from salmon.webui.validation import validate_album_dir, validate_writable_album_dir

    lib = tmp_path / "music"
    album = lib / "Artist" / "Album"
    album.mkdir(parents=True)
    staging = tmp_path / "staging"
    (staging / "Working").mkdir(parents=True)

    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])
    monkeypatch.setattr(cfg.directory, "download_directory", str(staging))

    # readable/uploadable either way
    assert validate_album_dir(str(album))
    # but not writable
    with pytest.raises(HTTPException) as exc:
        validate_writable_album_dir(str(album))
    assert exc.value.status_code == 403
    # staging stays writable
    assert validate_writable_album_dir(str(staging / "Working"))
