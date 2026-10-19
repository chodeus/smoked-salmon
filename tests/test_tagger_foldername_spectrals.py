"""A folder rename moves the spectrals salmon made to the new name; a folder it did not make stays."""

import hashlib
from pathlib import Path
from typing import Any

import asyncclick as click
import pytest

from salmon import cfg, dryrun
from salmon.tagger import foldername
from salmon.uploader.spectrals import create_specs_folder, get_spectrals_path

NEW_NAME = "Artist - Album (2020) [WEB FLAC]"
SPECTRAL_FILES = {"01 Full.png": b"full", "01 Zoom.png": b"zoom"}


def _metadata(scene: bool = False) -> dict[str, Any]:
    return {
        "scene": scene,
        "artists": [("Artist", "main")],
        "title": "Album",
        "year": 2020,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
    }


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """A download_directory and a place for albums that is not in it; no tmp_dir, no library, nothing removed."""
    downloads, seeding = tmp_path / "downloads", tmp_path / "seeding"
    downloads.mkdir()
    seeding.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", False)
    monkeypatch.setattr(foldername, "generate_folder_name", lambda _metadata: NEW_NAME)
    return downloads, seeding


def _album(folder: Path) -> Path:
    folder.mkdir()
    (folder / "01 - one.flac").write_bytes(b"one")
    (folder / "02 - two.flac").write_bytes(b"two")
    (folder / "cover.jpg").write_bytes(b"jpeg")
    return folder


def _write_spectrals(spectrals: Path) -> None:
    """Spectrals as salmon makes them: through create_specs_folder."""
    create_specs_folder("", str(spectrals))
    for name, data in SPECTRAL_FILES.items():
        (spectrals / name).write_bytes(data)


def _listing(folder: Path) -> dict[str, str]:
    """Every entry under folder, by relative path: a hash of its bytes, or "dir"."""
    return {
        str(entry.relative_to(folder)): hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else "dir"
        for entry in sorted(folder.rglob("*"))
    }


def _files(folder: Path) -> dict[str, bytes]:
    return {entry.name: entry.read_bytes() for entry in folder.iterdir()}


@pytest.mark.parametrize("hardlinks", [True, False])
def test_the_tmp_dir_spectrals_move_to_the_new_name(monkeypatch, dirs, tmp_path, hardlinks: bool) -> None:
    _downloads, seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(seeding / "Old Name")
    _write_spectrals(Path(get_spectrals_path(str(album))))

    new_path = foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    moved = Path(get_spectrals_path(new_path))
    assert Path(new_path).name == NEW_NAME
    assert moved.name.startswith(f"spectrals_{NEW_NAME} ")
    assert sorted(tmp_dir.iterdir()) == [moved]
    assert _files(moved) == SPECTRAL_FILES


def test_a_stale_tmp_dir_spectrals_folder_of_the_new_name_is_replaced(monkeypatch, dirs, tmp_path) -> None:
    downloads, seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    album = _album(seeding / "Old Name")
    _write_spectrals(Path(get_spectrals_path(str(album))))
    stale = Path(get_spectrals_path(str(downloads / NEW_NAME)))
    stale.mkdir()
    (stale / "01 Full.png").write_bytes(b"stale")

    foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    assert sorted(tmp_dir.iterdir()) == [stale]
    assert _files(stale) == SPECTRAL_FILES


def test_same_named_albums_get_their_own_tmp_dir_spectrals(monkeypatch, dirs, tmp_path) -> None:
    # Web jobs on two albums run at once: making one's spectrals must not replace the other's.
    _downloads, seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    (seeding / "A").mkdir()
    (seeding / "B").mkdir()
    first = Path(get_spectrals_path(str(_album(seeding / "A" / "CD1"))))
    _write_spectrals(first)

    made = Path(create_specs_folder(str(_album(seeding / "B" / "CD1"))))

    assert made.parent == tmp_dir
    assert _files(first) == SPECTRAL_FILES


def test_an_album_reached_through_a_link_has_one_tmp_dir_spectrals_folder(monkeypatch, dirs, tmp_path) -> None:
    downloads, seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    album = _album(downloads / "Album")
    (seeding / "Linked").symlink_to(album)

    assert get_spectrals_path(str(seeding / "Linked")) == get_spectrals_path(str(album))


@pytest.mark.parametrize("hardlinks", [True, False])
@pytest.mark.parametrize("scene", [False, True])
def test_the_source_spectrals_move_into_the_renamed_copy(monkeypatch, dirs, hardlinks: bool, scene: bool) -> None:
    downloads, seeding = dirs
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(seeding / "Old Name")
    before = _listing(album)
    _write_spectrals(Path(get_spectrals_path(str(album))))

    new_path = Path(foldername.rename_folder(str(album), _metadata(scene), auto_rename=True, check=False))

    assert new_path.parent == downloads
    assert new_path.name == ("Old Name" if scene else NEW_NAME)
    assert not (album / "Spectrals").exists()
    assert _listing(album) == before
    assert _files(Path(get_spectrals_path(str(new_path)))) == SPECTRAL_FILES
    assert {key: value for key, value in _listing(new_path).items() if not key.startswith("Spectrals")} == before


@pytest.mark.parametrize("hardlinks", [True, False])
def test_a_user_folder_named_spectrals_below_the_top_level_is_still_copied(monkeypatch, dirs, hardlinks: bool) -> None:
    _downloads, seeding = dirs
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(seeding / "Old Name")
    (album / "Extras" / "Spectrals").mkdir(parents=True)
    (album / "Extras" / "Spectrals" / "mine.png").write_bytes(b"mine")
    before = _listing(album)
    _write_spectrals(Path(get_spectrals_path(str(album))))

    new_path = Path(foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False))

    assert _listing(album) == before
    assert (new_path / "Extras" / "Spectrals" / "mine.png").read_bytes() == b"mine"
    assert _files(new_path / "Spectrals") == SPECTRAL_FILES


def test_an_album_without_spectrals_is_copied_as_before(dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Old Name")
    before = _listing(album)

    new_path = Path(foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False))

    assert _listing(album) == before
    assert _listing(new_path) == before


@pytest.mark.parametrize("remove_source_dir", [False, True])
def test_a_plain_file_named_spectrals_is_copied_like_any_other(monkeypatch, dirs, remove_source_dir: bool) -> None:
    _downloads, seeding = dirs
    monkeypatch.setattr(cfg.upload.formatting, "remove_source_dir", remove_source_dir)
    album = _album(seeding / "Old Name")
    (album / "Spectrals").write_bytes(b"not a folder")

    new_path = Path(foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False))

    assert (new_path / "Spectrals").read_bytes() == b"not a folder"


def test_a_library_album_keeps_its_own_spectrals_folder(monkeypatch, dirs) -> None:
    downloads, _seeding = dirs
    library = downloads.parent / "library"
    library.mkdir()
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    album = _album(library / "Old Name")
    _write_spectrals(album / "Spectrals")
    before = _listing(library)

    foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    assert _listing(library) == before


@pytest.mark.parametrize("hardlinks", [True, False])
def test_a_spectrals_folder_salmon_did_not_make_stays_in_the_source(monkeypatch, dirs, hardlinks: bool) -> None:
    # As with --spectrals-after or salmon tag: a seeding source's own Spectrals folder is copied, never moved.
    _downloads, seeding = dirs
    monkeypatch.setattr(cfg.directory, "hardlinks", hardlinks)
    album = _album(seeding / "Old Name")
    (album / "Spectrals").mkdir()
    (album / "Spectrals" / "theirs.png").write_bytes(b"theirs")
    before = _listing(album)

    new_path = Path(foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False))

    assert _listing(album) == before
    assert (new_path / "Spectrals" / "theirs.png").read_bytes() == b"theirs"


def test_a_second_rename_still_moves_salmons_spectrals(monkeypatch, dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Old Name")
    _write_spectrals(Path(get_spectrals_path(str(album))))
    first = Path(foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False))
    monkeypatch.setattr(foldername, "generate_folder_name", lambda _metadata: "Second Name")

    second = Path(foldername.rename_folder(str(first), _metadata(), auto_rename=True, check=False))

    assert not (first / "Spectrals").exists()
    assert _files(second / "Spectrals") == SPECTRAL_FILES


def test_a_folder_made_later_at_the_old_path_is_not_salmons(monkeypatch, dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Old Name")
    _write_spectrals(Path(get_spectrals_path(str(album))))
    foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)
    (album / "Spectrals").mkdir()
    (album / "Spectrals" / "theirs.png").write_bytes(b"theirs")
    before = _listing(album)
    monkeypatch.setattr(foldername, "generate_folder_name", lambda _metadata: "Second Name")

    foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    assert _listing(album) == before


def test_a_folder_made_where_salmon_removed_its_own_is_the_users(monkeypatch, tmp_path) -> None:
    import anyio

    from salmon.uploader import spectrals

    specs = tmp_path / "Album" / "Spectrals"
    specs.parent.mkdir()
    create_specs_folder("", str(specs))

    async def no_upload(*_args: Any) -> dict:
        return {}

    monkeypatch.setattr(spectrals, "upload_spectrals", no_upload)
    anyio.run(spectrals.handle_spectrals_upload_and_deletion, str(specs), None)
    specs.mkdir()

    assert not spectrals.made_by_salmon(str(specs))


def test_salmons_spectrals_given_another_file_are_not_deleted(monkeypatch, tmp_path, capsys) -> None:
    import anyio

    from salmon.uploader import spectrals

    specs = tmp_path / "Album" / "Spectrals"
    specs.parent.mkdir()
    _write_spectrals(specs)
    (specs / "notes.txt").write_bytes(b"theirs")

    async def no_upload(*_args: Any) -> dict:
        return {}

    monkeypatch.setattr(spectrals, "upload_spectrals", no_upload)
    anyio.run(spectrals.handle_spectrals_upload_and_deletion, str(specs), None)

    assert (specs / "notes.txt").read_bytes() == b"theirs"
    assert "Not deleting" in capsys.readouterr().out


def test_a_folder_given_another_file_before_the_second_try_is_not_deleted(monkeypatch, tmp_path, capsys) -> None:
    import shutil

    import anyio

    from salmon.uploader import spectrals

    specs = tmp_path / "Album" / "Spectrals"
    specs.parent.mkdir()
    _write_spectrals(specs)
    rmtree = shutil.rmtree
    tries: list[str] = []
    slept: list[float] = []

    def held_on_the_first_try(path: str, ignore_errors: bool = False) -> None:
        # As on Windows, where a viewer holding a file stops the first try.
        tries.append(path)
        if len(tries) > 1:
            rmtree(path, ignore_errors=ignore_errors)

    async def a_file_arrives(seconds: float) -> None:
        if not slept:
            (specs / "notes.txt").write_bytes(b"theirs")
        slept.append(seconds)

    async def no_upload(*_args: Any) -> dict:
        return {}

    monkeypatch.setattr(spectrals, "upload_spectrals", no_upload)
    monkeypatch.setattr(spectrals.shutil, "rmtree", held_on_the_first_try)
    monkeypatch.setattr(spectrals.anyio, "sleep", a_file_arrives)
    anyio.run(spectrals.handle_spectrals_upload_and_deletion, str(specs), None)

    assert (specs / "notes.txt").read_bytes() == b"theirs"
    assert len(tries) == 1
    assert "Not deleting" in capsys.readouterr().out


@pytest.mark.parametrize("put_there", ["a folder", "a link to another of salmon's"])
def test_what_takes_the_place_of_salmons_folder_during_the_delete_is_not_salmons(
    monkeypatch, tmp_path, put_there: str
) -> None:
    import anyio

    from salmon.uploader import spectrals

    specs = tmp_path / "Album" / "Spectrals"
    specs.parent.mkdir()
    _write_spectrals(specs)
    other = Path(create_specs_folder("", str(tmp_path / "spectrals_Other")))
    slept: list[float] = []

    async def put_in_its_place(seconds: float) -> None:
        if not slept:
            if put_there == "a folder":
                specs.mkdir()
                (specs / ".theirs").write_bytes(b"theirs")
                (specs / "notes.txt").write_bytes(b"theirs")
            else:
                specs.symlink_to(other)
        slept.append(seconds)

    async def no_upload(*_args: Any) -> dict:
        return {}

    monkeypatch.setattr(spectrals, "upload_spectrals", no_upload)
    monkeypatch.setattr(spectrals.anyio, "sleep", put_in_its_place)
    anyio.run(spectrals.handle_spectrals_upload_and_deletion, str(specs), None)

    if put_there == "a folder":
        # Left with only a hidden file of the user's, it must not pass for salmon's empty folder.
        (specs / "notes.txt").unlink()
        assert not spectrals.made_by_salmon(str(specs))
    else:
        assert spectrals.made_by_salmon(str(other))


def test_the_claim_goes_when_the_last_delete_try_raises(monkeypatch, tmp_path) -> None:
    import anyio

    from salmon.uploader import spectrals

    specs = tmp_path / "Album" / "Spectrals"
    specs.parent.mkdir()
    _write_spectrals(specs)

    def held(_path: str, ignore_errors: bool = False) -> None:
        if not ignore_errors:
            raise PermissionError("held by a viewer")

    async def no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(spectrals.shutil, "rmtree", held)
    monkeypatch.setattr(spectrals.anyio, "sleep", no_sleep)
    with pytest.raises(PermissionError):
        anyio.run(spectrals._delete_spectrals, str(specs))

    for entry in specs.iterdir():
        entry.unlink()
    # Empty, and no longer claimed: the user's.
    assert not spectrals.made_by_salmon(str(specs))


def test_the_spectral_urls_survive_a_delete_that_fails(monkeypatch, tmp_path, capsys) -> None:
    import anyio

    from salmon.uploader import spectrals

    specs = tmp_path / "Album" / "Spectrals"
    specs.parent.mkdir()
    _write_spectrals(specs)

    async def uploaded(_path, _ids):
        return {1: ["https://img.example/1.png"]}

    def held(_path: str, ignore_errors: bool = False) -> None:
        if not ignore_errors:
            raise PermissionError("held by a viewer")

    async def no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(spectrals, "upload_spectrals", uploaded)
    monkeypatch.setattr(spectrals.shutil, "rmtree", held)
    monkeypatch.setattr(spectrals.anyio, "sleep", no_sleep)
    urls = anyio.run(spectrals.handle_spectrals_upload_and_deletion, str(specs), {1: "01 Full.png"})

    assert urls == {1: ["https://img.example/1.png"]}
    assert "Could not delete" in capsys.readouterr().out


def test_a_library_albums_spectrals_follow_it_to_the_renamed_copy(monkeypatch, dirs) -> None:
    downloads, _seeding = dirs
    library = downloads.parent / "library"
    library.mkdir()
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(library)])
    album = _album(library / "Old Name")
    _write_spectrals(Path(get_spectrals_path(str(album))))

    new_path = foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    assert _files(Path(get_spectrals_path(new_path))) == SPECTRAL_FILES


def test_salmons_spectrals_beside_an_albums_own_follow_the_rename(dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Old Name")
    (album / "Spectrals").mkdir()
    (album / "Spectrals" / "theirs.png").write_bytes(b"theirs")
    _write_spectrals(Path(get_spectrals_path(str(album))))

    new_path = foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    assert (album / "Spectrals" / "theirs.png").read_bytes() == b"theirs"
    assert (Path(new_path) / "Spectrals" / "theirs.png").read_bytes() == b"theirs"
    assert _files(Path(get_spectrals_path(new_path))) == SPECTRAL_FILES


def test_a_file_named_spectrals_in_the_album_gets_salmons_folder_beside_it(dirs) -> None:
    downloads, seeding = dirs
    album = _album(seeding / "Album")
    (album / "Spectrals").write_bytes(b"not a folder")

    made = create_specs_folder(str(album))

    assert (album / "Spectrals").read_bytes() == b"not a folder"
    assert Path(made).parent == downloads


def test_a_scene_release_keeps_its_tmp_dir_spectrals_in_place(monkeypatch, dirs, tmp_path) -> None:
    downloads, _seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    album = _album(downloads / "Old Name")
    specs = Path(get_spectrals_path(str(album)))
    _write_spectrals(specs)

    foldername.rename_folder(str(album), _metadata(scene=True), auto_rename=True, check=False)

    assert _files(specs) == SPECTRAL_FILES


def test_a_dry_run_makes_spectrals_beside_an_albums_own_in_its_run_directory(dirs, tmp_path) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    (album / "Spectrals").mkdir()
    (album / "Spectrals" / "theirs.png").write_bytes(b"theirs")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with dryrun.mode(), dryrun.writing_into(str(run_dir)):
        made = create_specs_folder(str(album))

    assert Path(made).parent == run_dir


def test_spectrals_an_earlier_run_left_in_the_album_are_still_salmons(dirs) -> None:
    _downloads, seeding = dirs
    album = _album(seeding / "Album")
    left = album / "Spectrals"
    left.mkdir()
    for name in ("01 Full.png", "01 Zoom.png", "01 Frequency.png", ".DS_Store"):
        (left / name).write_bytes(b"old")

    made = create_specs_folder(str(album))

    assert Path(made) == left
    assert list(left.iterdir()) == []


def test_a_symlinked_spectrals_folder_is_never_salmons(dirs, tmp_path) -> None:
    downloads, seeding = dirs
    album = _album(seeding / "Album")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "01 Full.png").write_bytes(b"theirs")
    (album / "Spectrals").symlink_to(elsewhere)

    made = create_specs_folder(str(album))

    assert (elsewhere / "01 Full.png").read_bytes() == b"theirs"
    assert Path(made).parent == downloads


def test_salmons_tmp_dir_spectrals_follow_a_rename_into_the_same_folder(monkeypatch, dirs, tmp_path) -> None:
    downloads, seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    link = seeding / "Old Name"
    link.symlink_to(_album(downloads / NEW_NAME))
    _write_spectrals(Path(get_spectrals_path(str(link))))

    new_path = foldername.rename_folder(str(link), _metadata(), auto_rename=True, check=False)

    assert _files(Path(get_spectrals_path(new_path))) == SPECTRAL_FILES


def test_a_folder_of_the_new_name_holding_other_files_stops_the_rename(monkeypatch, dirs, tmp_path) -> None:
    downloads, seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    album = _album(seeding / "Old Name")
    ours = Path(get_spectrals_path(str(album)))
    _write_spectrals(ours)
    theirs = Path(get_spectrals_path(str(downloads / NEW_NAME)))
    theirs.mkdir()
    (theirs / "theirs.png").write_bytes(b"theirs")

    with pytest.raises(click.ClickException, match="files salmon did not make"):
        foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    assert (theirs / "theirs.png").read_bytes() == b"theirs"
    assert _files(ours) == SPECTRAL_FILES


def test_a_folder_at_the_old_name_that_is_not_salmons_stays_put(monkeypatch, dirs, tmp_path) -> None:
    _downloads, seeding = dirs
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    album = _album(seeding / "Old Name")
    theirs = Path(get_spectrals_path(str(album)))
    theirs.mkdir()
    (theirs / "theirs.png").write_bytes(b"theirs")

    new_path = foldername.rename_folder(str(album), _metadata(), auto_rename=True, check=False)

    assert (theirs / "theirs.png").read_bytes() == b"theirs"
    assert not Path(get_spectrals_path(new_path)).exists()


@pytest.mark.parametrize("theirs", ["a file", "a folder named like a spectral"])
def test_making_spectrals_over_a_folder_that_is_not_salmons_is_refused(tmp_path, theirs: str) -> None:
    # As when a web job's destination, chosen when it was queued, is taken by the time it runs.
    specs = tmp_path / "spectrals_Album"
    specs.mkdir()
    if theirs == "a file":
        (specs / "theirs.png").write_bytes(b"theirs")
    else:
        (specs / "01 Full.png").mkdir()
        (specs / "01 Full.png" / "theirs.png").write_bytes(b"theirs")

    with pytest.raises(click.ClickException, match="files salmon did not make"):
        create_specs_folder("", str(specs))

    assert len(list(specs.rglob("theirs.png"))) == 1


@pytest.mark.parametrize("entry", ["folder", "link"])
def test_a_hidden_folder_or_link_beside_salmons_images_is_not_replaced(tmp_path, entry: str) -> None:
    specs = tmp_path / "spectrals_Album"
    _write_spectrals(specs)
    hidden = specs / ".theirs"
    if entry == "folder":
        hidden.mkdir()
        (hidden / "notes.txt").write_bytes(b"theirs")
    else:
        (tmp_path / "elsewhere").mkdir()
        hidden.symlink_to(tmp_path / "elsewhere")

    with pytest.raises(click.ClickException, match="files salmon did not make"):
        create_specs_folder("", str(specs))

    assert hidden.exists()
