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


def test_a_library_given_in_another_case_is_still_the_library(tmp_path, monkeypatch) -> None:
    lib = tmp_path / "Music"
    (lib / "Artist" / "Album").mkdir(parents=True)
    other_case = tmp_path / "music" / "Artist" / "Album"
    if not other_case.is_dir():
        pytest.skip("this filesystem is case-sensitive")
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    assert cfg.directory.is_library_path(str(other_case))
    assert cfg.directory.library_inside(str(tmp_path / "music")) == str(lib)


def test_a_library_reached_by_a_path_no_string_matches_is_still_the_library(tmp_path, monkeypatch) -> None:
    from salmon.config import validations

    lib = tmp_path / "music"
    (lib / "Album").mkdir(parents=True)
    (tmp_path / "mount").symlink_to(lib)
    # Stands in for a second bind mount: the strings name another folder, the device and inode are the library's.
    monkeypatch.setattr(validations, "_real", validations._lexical)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    assert cfg.directory.is_library_path(str(tmp_path / "mount" / "Album"))


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


def test_library_source_is_staged_as_a_real_copy_in_a_run_directory(tmp_path, monkeypatch) -> None:
    # A hardlink shares the inode, so tag writes would reach the library file.
    import os

    from salmon.uploader.staging import STAGING_DIR, staged_source

    lib = tmp_path / "music"
    album = lib / "Artist - Album"
    album.mkdir(parents=True)
    track = album / "01.flac"
    track.write_bytes(b"fLaC-original")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))

    with staged_source(str(album), scratch=False) as (dest, rename_into):
        copied = os.path.join(dest, "01.flac")
        assert os.path.dirname(os.path.dirname(dest)) == str(downloads / STAGING_DIR)
        assert rename_into is None, "the renamed copy goes into download_directory, to be seeded"
        assert os.stat(copied).st_ino != os.stat(track).st_ino
        with open(copied, "wb") as fh:
            fh.write(b"retagged")
        assert track.read_bytes() == b"fLaC-original"
    assert os.listdir(downloads / STAGING_DIR) == [], "the run directory goes when the run ends"


def test_staging_never_touches_a_folder_of_the_same_name(tmp_path, monkeypatch) -> None:
    from salmon.uploader.staging import staged_source

    lib = tmp_path / "music"
    (lib / "Album").mkdir(parents=True)
    downloads = tmp_path / "downloads"
    (downloads / "Album").mkdir(parents=True)
    (downloads / "Album" / "mine.flac").write_bytes(b"mine")
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))

    with staged_source(str(lib / "Album"), scratch=False) as (dest, _rename_into):
        assert dest != str(downloads / "Album")
    assert (downloads / "Album" / "mine.flac").read_bytes() == b"mine"


def test_staging_refuses_a_folder_that_holds_a_library(tmp_path, monkeypatch) -> None:
    from salmon.errors import UploadError
    from salmon.uploader.staging import staged_source

    lib = tmp_path / "media" / "music"
    lib.mkdir(parents=True)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    with pytest.raises(UploadError, match="holds the library folder"), staged_source(str(tmp_path / "media"), False):
        pass


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


async def test_tag_endpoint_takes_a_library_album_but_not_a_folder_holding_one(tmp_path, monkeypatch) -> None:
    """`salmon tag` works on a copy of a library album; a folder holding a library is never one album."""
    import fastapi
    import pytest as _pytest

    from salmon.webui.routers import tools

    downloads = tmp_path / "downloads"
    lib = downloads / "music"
    album = lib / "Artist" / "Album"
    album.mkdir(parents=True)
    # A library inside download_directory fails config validation; set here only to reach the holding case.
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    called: list[str] = []
    monkeypatch.setattr(tools, "_TAG", lambda **_kw: called.append("tag"))

    def queue(*_a, **_kw):
        called.append("queue")
        return {"id": "job"}

    monkeypatch.setattr(tools, "_queue", queue)

    await tools.tag(tools.TagRequest(path=str(album), source="CD"))
    assert called == ["queue"]

    with _pytest.raises(fastapi.HTTPException) as exc:
        await tools.tag(tools.TagRequest(path=str(downloads), source="CD"))
    assert exc.value.status_code == 403
    assert called == ["queue"], "a folder holding a library is never queued"


def test_a_trackers_own_dottorrents_dir_inside_a_library_is_refused(tmp_path) -> None:
    from salmon.config.validations import Cfg, GazelleTrackerSettings, Tracker

    lib = tmp_path / "music"
    (lib / "torrents").mkdir(parents=True)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    directory = Directory(dottorrents_dir=str(downloads), download_directory=str(downloads), library_dirs=[str(lib)])
    inside = Tracker(red=GazelleTrackerSettings(session="placeholder", dottorrents_dir=str(lib / "torrents")))
    beside = Tracker(red=GazelleTrackerSettings(session="placeholder", dottorrents_dir=str(downloads)))

    with pytest.raises(ValueError, match="tracker.red.dottorrents_dir"):
        Cfg(directory=directory, tracker=inside)
    assert Cfg(directory=directory, tracker=beside).tracker.red is not None


def test_staging_an_album_with_a_dangling_symlink_says_why(tmp_path, monkeypatch) -> None:
    from salmon.errors import UploadError
    from salmon.uploader.staging import staged_source

    lib = tmp_path / "music"
    album = lib / "Album"
    album.mkdir(parents=True)
    (album / "01.flac").symlink_to(tmp_path / "gone.flac")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))

    with pytest.raises(UploadError, match="Could not measure"), staged_source(str(album), scratch=False):
        pass
