"""library_dirs: browsable/uploadable sources that must never be deleted."""

import inspect

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


def test_filesystem_root_as_library_dir_still_contains_descendants(monkeypatch) -> None:
    # "root + os.sep" is "//" when root is "/", so a naive prefix check would call
    # /data/album non-library and let the abort handler rmtree it.
    monkeypatch.setattr(cfg.directory, "library_dirs", ["/"])
    assert cfg.directory.is_library_path("/data/album")
    assert cfg.directory.is_library_path("/")


def test_is_within_roots_handles_filesystem_root(monkeypatch) -> None:
    from salmon.webui.validation import is_within_roots

    assert is_within_roots("/data/album", ["/"])
    assert not is_within_roots("/data/music-old", ["/data/music"])


def test_library_source_is_staged_as_a_real_copy(tmp_path, monkeypatch) -> None:
    # A hardlink shares the inode, so tag writes would reach the library file.
    # Staging must produce an independent copy.
    import os

    from salmon.uploader import _stage_library_source

    lib = tmp_path / "music"
    album = lib / "Artist - Album"
    album.mkdir(parents=True)
    track = album / "01.flac"
    track.write_bytes(b"fLaC-original")
    staging = tmp_path / "staging"
    staging.mkdir()

    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])
    monkeypatch.setattr(cfg.directory, "download_directory", str(staging))

    dest = _stage_library_source(str(album))

    assert dest == str(staging / "Artist - Album")
    copied = staging / "Artist - Album" / "01.flac"
    assert copied.read_bytes() == b"fLaC-original"
    # the decisive assertion: separate inode, so writing the copy cannot touch the library
    assert os.stat(copied).st_ino != os.stat(track).st_ino

    copied.write_bytes(b"retagged")
    assert track.read_bytes() == b"fLaC-original"


def test_staging_refuses_to_clobber_an_existing_folder(tmp_path, monkeypatch) -> None:
    from salmon.errors import UploadError
    from salmon.uploader import _stage_library_source

    lib = tmp_path / "music"
    album = lib / "Album"
    album.mkdir(parents=True)
    staging = tmp_path / "staging"
    (staging / "Album").mkdir(parents=True)

    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])
    monkeypatch.setattr(cfg.directory, "download_directory", str(staging))

    with pytest.raises(UploadError, match="already exists"):
        _stage_library_source(str(album))


@pytest.mark.parametrize("field", ["download_directory", "dottorrents_dir", "tmp_dir"])
def test_library_dir_containing_a_writable_dir_is_rejected(tmp_path, field) -> None:
    # library_dirs = ["/data"] with download_directory = "/data/torrents/salmon" would
    # stage into the library and mark every staging album read-only. Fail at load.
    lib = tmp_path / "data"
    inner = lib / "torrents" / "salmon"
    inner.mkdir(parents=True)
    kwargs = {
        "dottorrents_dir": str(tmp_path / "elsewhere"),
        "download_directory": str(tmp_path / "elsewhere"),
        "library_dirs": [str(lib)],
    }
    (tmp_path / "elsewhere").mkdir(exist_ok=True)
    kwargs[field] = str(inner)

    with pytest.raises(ValueError, match="must not contain"):
        Directory(**kwargs)


def test_library_dir_beside_the_writable_dirs_is_accepted(tmp_path) -> None:
    lib = tmp_path / "media" / "music"
    lib.mkdir(parents=True)
    staging = tmp_path / "torrents" / "salmon"
    staging.mkdir(parents=True)

    directory = Directory(
        dottorrents_dir=str(staging),
        download_directory=str(staging),
        library_dirs=[str(lib)],
    )
    assert directory.library_dirs == [str(lib)]


def test_tag_endpoint_refuses_a_library_source(tmp_path, monkeypatch) -> None:
    """`salmon tag` saves over the source files and renames the folder, so the
    web endpoint must reject a read-only library album the way convert does."""
    import fastapi
    import pytest as _pytest

    from salmon.webui.routers import tools

    lib = tmp_path / "music"
    album = lib / "Artist" / "Album"
    album.mkdir(parents=True)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    with _pytest.raises(fastapi.HTTPException) as exc:
        tools.validate_writable_album_dir(str(album))
    assert exc.value.status_code == 403

    # and the endpoint is wired to that validator, not the permissive one
    source = inspect.getsource(tools.tag)
    assert "validate_writable_album_dir(" in source
    assert "validate_album_dir(" not in source.replace("validate_writable_album_dir(", "")
