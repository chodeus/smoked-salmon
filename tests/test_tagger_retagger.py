import os
from types import SimpleNamespace

import pytest

from salmon import cfg
from salmon.errors import UploadError
from salmon.tagger.retagger import (
    Change,
    _get_tag_number,
    _remap_spectral_ids,
    create_track_changes,
    move_non_audio_files,
    rename_files,
)


def test_rename_files_can_flatten_multi_disc_tracks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)

    (tmp_path / "01.flac").write_text("a")
    (tmp_path / "02.flac").write_text("b")

    tags = {
        "01.flac": SimpleNamespace(tracknumber="01", discnumber="1"),
        "02.flac": SimpleNamespace(tracknumber="01", discnumber="2"),
    }
    metadata = {
        "tracks": {
            "1": {"1": {"artists": [("Artist", "main")], "title": "Track 1"}},
            "2": {"1": {"artists": [("Artist", "main")], "title": "Track 2"}},
        }
    }

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert (tmp_path / "1.01.flac").exists()
    assert (tmp_path / "2.01.flac").exists()
    assert not (tmp_path / "CD01").exists()
    assert not (tmp_path / "CD02").exists()


def test_rename_files_swap_preserves_content(tmp_path, monkeypatch) -> None:
    # Files are mis-numbered so retag swaps their names (01<->02). A single-phase
    # os.rename would clobber a source before its content moved; two-phase must
    # preserve every file's bytes across the swap.
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)

    (tmp_path / "01.flac").write_text("A")  # tagged track 2 -> target 02.flac
    (tmp_path / "02.flac").write_text("B")  # tagged track 1 -> target 01.flac

    tags = {
        "01.flac": SimpleNamespace(tracknumber="02", discnumber="1"),
        "02.flac": SimpleNamespace(tracknumber="01", discnumber="1"),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": {"artists": [("Artist", "main")], "title": "Track 1"},
                "2": {"artists": [("Artist", "main")], "title": "Track 2"},
            }
        }
    }

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert (tmp_path / "01.flac").read_text() == "B"
    assert (tmp_path / "02.flac").read_text() == "A"
    assert not list(tmp_path.glob(".salmon-rename-*"))  # no temp files left behind


def test_rename_files_rolls_back_on_midway_failure(tmp_path, monkeypatch) -> None:
    # A failure mid-phase-2 must restore every file to its original name — no
    # bare staging indices, no half-renamed layout.
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)

    (tmp_path / "01.flac").write_text("A")
    (tmp_path / "02.flac").write_text("B")
    tags = {
        "01.flac": SimpleNamespace(tracknumber="02", discnumber="1"),
        "02.flac": SimpleNamespace(tracknumber="01", discnumber="1"),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": {"artists": [("Artist", "main")], "title": "Track 1"},
                "2": {"artists": [("Artist", "main")], "title": "Track 2"},
            }
        }
    }

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 4:  # the second phase-2 move
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr("salmon.tagger.retagger.os.replace", flaky_replace)

    with pytest.raises(OSError, match="disk full"):
        rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert (tmp_path / "01.flac").read_text() == "A"
    assert (tmp_path / "02.flac").read_text() == "B"
    assert not list(tmp_path.glob(".salmon-rename-*"))


def test_rename_files_refuses_target_onto_untracked_file(tmp_path, monkeypatch) -> None:
    # A file on disk but absent from tags would be overwritten in phase 2 if a
    # target lands on its name — the collision check must refuse up front.
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)

    (tmp_path / "01.flac").write_text("A")  # tagged track 2 -> target 02.flac
    (tmp_path / "02.flac").write_text("UNTRACKED")  # exists on disk, not in tags

    tags = {"01.flac": SimpleNamespace(tracknumber="02", discnumber="1")}
    metadata = {"tracks": {"1": {"1": {"artists": [("Artist", "main")], "title": "Track 1"}}}}

    with pytest.raises(UploadError):
        rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert (tmp_path / "01.flac").read_text() == "A"
    assert (tmp_path / "02.flac").read_text() == "UNTRACKED"


def test_rename_files_case_only_rename_is_not_a_collision(tmp_path, monkeypatch) -> None:
    # On a case-insensitive fs the target "01.flac" resolves to the source
    # "01.FLAC" itself; that file vacates in phase 1 and must not be refused.
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)

    (tmp_path / "01.FLAC").write_text("A")

    tags = {"01.FLAC": SimpleNamespace(tracknumber="01", discnumber="1")}
    metadata = {"tracks": {"1": {"1": {"artists": [("Artist", "main")], "title": "Track 1"}}}}

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert (tmp_path / "01.flac").read_text() == "A"


def test_rename_files_does_not_clobber_stale_staging_file(tmp_path, monkeypatch) -> None:
    # Staging uses a freshly reserved directory, so a leftover ".salmon-rename-*"
    # file from a crashed run can never be overwritten by a staging move.
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)

    (tmp_path / "01.flac").write_text("A")  # tagged track 2 -> target 02.flac
    (tmp_path / ".salmon-rename-stale").write_text("S")

    tags = {"01.flac": SimpleNamespace(tracknumber="02", discnumber="1")}
    metadata = {"tracks": {"1": {"1": {"artists": [("Artist", "main")], "title": "Track 1"}}}}

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert (tmp_path / "02.flac").read_text() == "A"
    assert (tmp_path / ".salmon-rename-stale").read_text() == "S"


def test_rename_files_uppercase_ext_audio_not_moved_as_non_audio(tmp_path, monkeypatch) -> None:
    # move_non_audio_files compares file.lower().endswith(ext): the stored ext must be
    # lowercased too, or leftover .FLAC audio in a disc folder is "non-audio" and moved.
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)

    for disc in ("CD1", "CD2"):
        (tmp_path / disc).mkdir()
        (tmp_path / disc / "01.FLAC").write_text(disc)
    (tmp_path / "CD1" / "bonus.FLAC").write_text("bonus")  # untracked audio stays put

    tags = {
        "CD1/01.FLAC": SimpleNamespace(tracknumber="01", discnumber="1"),
        "CD2/01.FLAC": SimpleNamespace(tracknumber="01", discnumber="2"),
    }
    metadata = {
        "tracks": {
            "1": {"1": {"artists": [("Artist", "main")], "title": "Track 1"}},
            "2": {"1": {"artists": [("Artist", "main")], "title": "Track 2"}},
        }
    }

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert (tmp_path / "1.01.flac").read_text() == "CD1"
    assert (tmp_path / "2.01.flac").read_text() == "CD2"
    assert (tmp_path / "CD1" / "bonus.FLAC").read_text() == "bonus"
    assert not (tmp_path / "bonus.FLAC").exists()
    assert not list(tmp_path.glob("bonus.*.FLAC"))  # not disc-suffixed into the root


def test_move_non_audio_files_does_not_overwrite_same_named_dest(tmp_path) -> None:
    # shutil.move silently overwrites: a cover.jpg both in the disc folder and the
    # destination must be suffixed, not clobbered.
    cd1 = tmp_path / "CD1"
    cd1.mkdir()
    (cd1 / "cover.jpg").write_text("disc")
    (tmp_path / "cover.jpg").write_text("root")

    move_non_audio_files({(".flac", str(cd1), str(tmp_path))})

    assert (tmp_path / "cover.jpg").read_text() == "root"
    assert (tmp_path / "cover.1.jpg").read_text() == "disc"


def test_move_non_audio_files_same_dir_is_a_noop(tmp_path) -> None:
    # old_dir == new_dir (renames within one disc folder): files stay untouched,
    # not spuriously suffixed by the overwrite guard.
    cd1 = tmp_path / "CD1"
    cd1.mkdir()
    (cd1 / "cover.jpg").write_text("disc")

    move_non_audio_files({(".flac", str(cd1), str(cd1))})

    assert (cd1 / "cover.jpg").read_text() == "disc"
    assert not (cd1 / "cover.1.jpg").exists()


def test_remap_spectral_ids_does_not_chain():
    # to_rename forms a chain a->b, b->c. Track a's spectral must land on b (its
    # direct target), not be dragged through to c by sequential application.
    spectral_ids = {1: "a.flac", 2: "b.flac"}
    _remap_spectral_ids(spectral_ids, [("a.flac", "b.flac"), ("b.flac", "c.flac")])
    assert spectral_ids == {1: "b.flac", 2: "c.flac"}


def _trackmeta(title, track_no, disc_no):
    return {
        "title": title,
        "isrc": None,
        "track#": track_no,
        "disc#": disc_no,
        "tracktotal": None,
        "disctotal": None,
        "artists": [("Some Artist", "main")],
    }


def _tagset(title, tracknumber, discnumber):
    return SimpleNamespace(
        artist=["Some Artist"],
        title=title,
        composer=None,
        conductor=None,
        comment=None,
        isrc=None,
        tracknumber=tracknumber,
        discnumber=discnumber,
        tracktotal=None,
        disctotal=None,
    )


def test_create_track_changes_matches_files_by_disc_and_track_number():
    # Tags arrive out of disc/track order, as os.walk gives them; a positional
    # zip against the tracklist would pair files with the wrong track.
    tags = {
        "1-02 Second.flac": _tagset("Old Second", tracknumber="2", discnumber="1"),
        "1-01 First.flac": _tagset("Old First", tracknumber="1", discnumber="1"),
        "2-01 Third.flac": _tagset("Old Third", tracknumber="1", discnumber="2"),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New First", "1", "1"),
                "2": _trackmeta("New Second", "2", "1"),
            },
            "2": {
                "1": _trackmeta("New Third", "1", "2"),
            },
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old First", "New First") in changes["1-01 First.flac"]
    assert Change("title", "Old Second", "New Second") in changes["1-02 Second.flac"]
    assert Change("title", "Old Third", "New Third") in changes["2-01 Third.flac"]


def test_get_tag_number_reads_the_number_part_of_a_slash_pair():
    assert _get_tag_number(SimpleNamespace(discnumber="3/12"), "discnumber") == 3


def test_get_tag_number_defaults_missing_tags_to_one():
    assert _get_tag_number(SimpleNamespace(discnumber=None), "discnumber") == 1
    assert _get_tag_number({}, "discnumber") == 1


def test_get_tag_number_unwraps_a_list_value():
    assert _get_tag_number({"tracknumber": ["7"]}, "tracknumber") == 7
