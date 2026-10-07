import os
from types import SimpleNamespace

import pytest

from salmon import cfg
from salmon.errors import UploadError
from salmon.tagger.retagger import (
    Change,
    _get_tag_number,
    _natural_key,
    _remap_spectral_ids,
    create_track_changes,
    move_non_audio_files,
    rename_files,
    tag_files,
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


def test_create_track_changes_uses_path_order_when_discnumber_tags_are_missing():
    # No DISCNUMBER anywhere: CD1 and CD2 track 1 both key as (1, 1), so the
    # paths, in natural order, decide instead of the colliding sort.
    tags = {
        "CD1/01.flac": _tagset("Old CD1 1", tracknumber="1", discnumber=None),
        "CD1/02.flac": _tagset("Old CD1 2", tracknumber="2", discnumber=None),
        "CD2/01.flac": _tagset("Old CD2 1", tracknumber="1", discnumber=None),
        "CD2/02.flac": _tagset("Old CD2 2", tracknumber="2", discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New CD1 1", "1", "1"),
                "2": _trackmeta("New CD1 2", "2", "1"),
            },
            "2": {
                "1": _trackmeta("New CD2 1", "1", "2"),
                "2": _trackmeta("New CD2 2", "2", "2"),
            },
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old CD1 1", "New CD1 1") in changes["CD1/01.flac"]
    assert Change("title", "Old CD1 2", "New CD1 2") in changes["CD1/02.flac"]
    assert Change("title", "Old CD2 1", "New CD2 1") in changes["CD2/01.flac"]
    assert Change("title", "Old CD2 2", "New CD2 2") in changes["CD2/02.flac"]


def test_the_file_order_fallback_puts_disc_10_after_disc_2():
    # gather_tags lists CD10 before CD2 (a path with no leading number sorts as text).
    tags = {f"CD{disc}/01.flac": _tagset(f"Old CD{disc}", tracknumber="1", discnumber=None) for disc in (1, 10, 2)}
    metadata = {"tracks": {str(disc): {"1": _trackmeta(f"New CD{disc}", "1", str(disc))} for disc in (1, 2, 10)}}

    changes = create_track_changes(tags, metadata)

    for disc in (1, 2, 10):
        assert Change("title", f"Old CD{disc}", f"New CD{disc}") in changes[f"CD{disc}/01.flac"]


def _two_discs_of_two():
    return {
        "tracks": {
            str(disc): {str(track): _trackmeta(f"New {disc}-{track}", str(track), str(disc)) for track in (1, 2)}
            for disc in (1, 2)
        }
    }


def test_a_flat_folder_without_disc_tags_is_refused_not_guessed():
    # Track-first names in one folder: no path order lines up with disc-first metadata.
    tags = {
        name: _tagset(f"Old {name}", tracknumber=name[1], discnumber=None)
        for name in ("01-CD1.flac", "01-CD2.flac", "02-CD1.flac", "02-CD2.flac")
    }
    with pytest.raises(UploadError, match="DISCNUMBER and TRACKNUMBER"):
        create_track_changes(tags, _two_discs_of_two())


def test_disc_folders_that_do_not_match_the_discs_are_refused():
    tags = {
        "CD1/01.flac": _tagset("a", tracknumber="1", discnumber=None),
        "CD1/02.flac": _tagset("b", tracknumber="2", discnumber=None),
        "CD1/03.flac": _tagset("c", tracknumber="3", discnumber=None),
        "CD2/01.flac": _tagset("d", tracknumber="1", discnumber=None),
    }
    with pytest.raises(UploadError, match="DISCNUMBER and TRACKNUMBER"):
        create_track_changes(tags, _two_discs_of_two())


def test_disc_folders_pair_by_track_tag_not_file_name():
    tags = {
        "CD1/b.flac": _tagset("Old 1-2", tracknumber="2", discnumber=None),
        "CD1/a.flac": _tagset("Old 1-1", tracknumber="1", discnumber=None),
        "CD2/z.flac": _tagset("Old 2-1", tracknumber="1", discnumber=None),
        "CD2/y.flac": _tagset("Old 2-2", tracknumber="2", discnumber=None),
    }
    changes = create_track_changes(tags, _two_discs_of_two())
    for name, title in (("CD1/a.flac", "1-1"), ("CD1/b.flac", "1-2"), ("CD2/z.flac", "2-1"), ("CD2/y.flac", "2-2")):
        assert Change("title", f"Old {title}", f"New {title}") in changes[name]


def test_one_disc_without_track_tags_pairs_by_file_name():
    tags = {f"{n:02d} x.flac": _tagset(f"Old {n}", tracknumber=None, discnumber=None) for n in (1, 10, 2)}
    metadata = {"tracks": {"1": {str(n): _trackmeta(f"New {n}", str(n), "1") for n in (1, 2, 10)}}}
    changes = create_track_changes(tags, metadata)
    for n in (1, 2, 10):
        assert Change("title", f"Old {n}", f"New {n}") in changes[f"{n:02d} x.flac"]


def _new_titles(changes) -> dict[str, str]:
    return {
        name: change.new for name, file_changes in changes.items() for change in file_changes if change.tag == "title"
    }


def test_a_disc_whose_track_tags_repeat_is_refused():
    # b.flac's track 1 is unambiguous, so file-name order must not override it.
    tags = {
        name: _tagset(f"Old {name}", tracknumber=n, discnumber="1")
        for name, n in (("a.flac", "3"), ("b.flac", "1"), ("z.flac", "3"))
    }
    metadata = {"tracks": {"1": {str(n): _trackmeta(f"New {n}", str(n), "1") for n in range(1, 4)}}}
    with pytest.raises(UploadError, match="track"):
        create_track_changes(tags, metadata)


@pytest.mark.parametrize("bad", [None, "N/A"], ids=["missing", "malformed"])
def test_a_disc_folder_with_an_unusable_track_tag_is_refused(bad):
    # CD1's usable numbers are unique, so only the unusable tag itself can stop the pairing.
    tags = {
        "CD1/a.flac": _tagset("a", tracknumber="2", discnumber=None),
        "CD1/b.flac": _tagset("b", tracknumber=bad, discnumber=None),
        "CD1/c.flac": _tagset("c", tracknumber="3", discnumber=None),
        "CD2/d.flac": _tagset("d", tracknumber="2", discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {str(n): _trackmeta(f"New 1-{n}", str(n), "1") for n in (1, 2, 3)},
            "2": {"1": _trackmeta("New 2-1", "1", "2")},
        }
    }
    with pytest.raises(UploadError, match="track"):
        create_track_changes(tags, metadata)


def test_a_malformed_track_tag_is_not_taken_as_track_1():
    # Unique (disc, track) pairs only because "N/A" reads as 1; the file names decide, and agree with b's tag.
    tags = {
        "b.flac": _tagset("b", tracknumber="2", discnumber=None),
        "a.flac": _tagset("a", tracknumber="N/A", discnumber=None),
    }
    metadata = {"tracks": {"1": {str(n): _trackmeta(f"New {n}", str(n), "1") for n in (1, 2)}}}
    changes = create_track_changes(tags, metadata)
    assert _new_titles(changes) == {"a.flac": "New 1", "b.flac": "New 2"}


def test_a_folder_whose_track_tags_all_repeat_is_ordered_by_file_name():
    # Every file tagged track 1, the reason to retag: no tag vouches for a place, so the names decide.
    tags = {name: _tagset(name, tracknumber="1", discnumber=None) for name in ("02 b.flac", "01 a.flac", "10 c.flac")}
    metadata = {"tracks": {"1": {str(n): _trackmeta(f"New {n}", str(n), "1") for n in (1, 2, 3)}}}
    changes = create_track_changes(tags, metadata)
    assert _new_titles(changes) == {"01 a.flac": "New 1", "02 b.flac": "New 2", "10 c.flac": "New 3"}


@pytest.mark.parametrize(
    "names",
    [("01.flac", "1.flac"), ("CD01/01.flac", "CD1/01.flac")],
    ids=["files", "disc folders"],
)
def test_names_that_sort_the_same_are_refused(names):
    # Without track tags the names decide, and these two can't be told apart.
    tags = {name: _tagset(name, tracknumber=None, discnumber=None) for name in names}
    discs = {str(d): {"1": _trackmeta(f"New {d}", "1", str(d))} for d in (1, 2)} if "/" in names[0] else None
    metadata = {"tracks": discs or {"1": {str(n): _trackmeta(f"New {n}", str(n), "1") for n in (1, 2)}}}
    with pytest.raises(UploadError, match="track"):
        create_track_changes(tags, metadata)


def test_natural_key_orders_digit_runs_as_numbers():
    paths = ["CD10/01.flac", "Disc 2/10.flac", "CD2/01.flac", "Disc 2/9.flac", "CD1/01.flac"]
    assert sorted(paths, key=_natural_key) == [
        "CD1/01.flac",
        "CD2/01.flac",
        "CD10/01.flac",
        "Disc 2/9.flac",
        "Disc 2/10.flac",
    ]


def test_get_tag_number_reads_the_number_part_of_a_slash_pair():
    assert _get_tag_number(SimpleNamespace(discnumber="3/12"), "discnumber") == 3


def test_get_tag_number_defaults_missing_tags_to_one():
    assert _get_tag_number(SimpleNamespace(discnumber=None), "discnumber") == 1
    assert _get_tag_number({}, "discnumber") == 1


def test_get_tag_number_unwraps_a_list_value():
    assert _get_tag_number({"tracknumber": ["7"]}, "tracknumber") == 7


# Ported from upstream (smokin-salmon/smoked-salmon#520).


def test_create_track_changes_keeps_file_order_when_discnumber_tags_are_missing():
    # CD1/CD2 folders, no DISCNUMBER: pairs collide across discs as (1, 1), so the files keep
    # their existing (correct) order instead of an untrustworthy sort.
    tags = {
        "CD1/01.flac": _tagset("Old CD1 1", tracknumber="1", discnumber=None),
        "CD1/02.flac": _tagset("Old CD1 2", tracknumber="2", discnumber=None),
        "CD2/01.flac": _tagset("Old CD2 1", tracknumber="1", discnumber=None),
        "CD2/02.flac": _tagset("Old CD2 2", tracknumber="2", discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New CD1 1", "1", "1"),
                "2": _trackmeta("New CD1 2", "2", "1"),
            },
            "2": {
                "1": _trackmeta("New CD2 1", "1", "2"),
                "2": _trackmeta("New CD2 2", "2", "2"),
            },
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old CD1 1", "New CD1 1") in changes["CD1/01.flac"]
    assert Change("title", "Old CD1 2", "New CD1 2") in changes["CD1/02.flac"]
    assert Change("title", "Old CD2 1", "New CD2 1") in changes["CD2/01.flac"]
    assert Change("title", "Old CD2 2", "New CD2 2") in changes["CD2/02.flac"]


def test_create_track_changes_falls_back_when_only_some_files_carry_a_discnumber_tag():
    # CD1's file has a DISCNUMBER/TRACKNUMBER pair, CD2's none: a (disc, track) sort would swap them,
    # so a mix of files with and without DISCNUMBER pairs by folder.
    tags = {
        "CD1/01.flac": _tagset("Old CD1", tracknumber="5", discnumber="1"),
        "CD2/01.flac": _tagset("Old CD2", tracknumber="1", discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {"1": _trackmeta("New CD1", "1", "1")},
            "2": {"1": _trackmeta("New CD2", "1", "2")},
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old CD1", "New CD1") in changes["CD1/01.flac"]
    assert Change("title", "Old CD2", "New CD2") in changes["CD2/01.flac"]


def test_create_track_changes_trusts_continuous_track_numbers_with_no_discnumber_tag_anywhere():
    # One flat folder, two discs, no DISCNUMBER, TRACKNUMBER counting through both: the keys are
    # unique, so the plain tag sort retags it with no folder fallback.
    tags = {f"{n:02d}.flac": _tagset(f"Old {n}", tracknumber=str(n), discnumber=None) for n in range(1, 7)}
    metadata = {
        "tracks": {
            "1": {str(t): _trackmeta(f"New 1-{t}", str(t), "1") for t in range(1, 4)},
            "2": {str(t): _trackmeta(f"New 2-{t}", str(t), "2") for t in range(1, 4)},
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old 4", "New 2-1") in changes["04.flac"]


def test_create_track_changes_falls_back_when_tag_pairs_do_not_match_the_metadata_discs():
    # Unique, parseable pairs that are not the metadata's (disc 1 has no track 3), in one folder:
    # zipping them would mispair, so this refuses.
    tags = {
        "a.flac": _tagset("Old a", tracknumber="1", discnumber="1"),
        "b.flac": _tagset("Old b", tracknumber="2", discnumber="1"),
        "c.flac": _tagset("Old c", tracknumber="3", discnumber="1"),
        "d.flac": _tagset("Old d", tracknumber="1", discnumber="2"),
    }
    metadata = {
        "tracks": {
            "1": {"1": _trackmeta("New 1-1", "1", "1"), "2": _trackmeta("New 1-2", "2", "1")},
            "2": {"1": _trackmeta("New 2-1", "1", "2"), "2": _trackmeta("New 2-2", "2", "2")},
        }
    }

    with pytest.raises(UploadError, match="DISCNUMBER"):
        create_track_changes(tags, metadata)


def test_create_track_changes_orders_ten_plus_discs_naturally():
    # No DISCNUMBER, so every file collides: the fallback orders disc folders naturally (CD2 before CD10).
    tags = {f"CD{disc}/01.flac": _tagset(f"Old CD{disc}", tracknumber="1", discnumber=None) for disc in (1, 10, 2)}
    metadata = {"tracks": {str(disc): {"1": _trackmeta(f"New CD{disc}", "1", str(disc))} for disc in (1, 2, 10)}}

    changes = create_track_changes(tags, metadata)

    for disc in (1, 2, 10):
        assert Change("title", f"Old CD{disc}", f"New CD{disc}") in changes[f"CD{disc}/01.flac"]


def test_create_track_changes_refuses_a_flat_folder_without_disc_tags():
    # Track-first names in one flat folder interleave the discs: neither tags nor folders sort them,
    # so retagging refuses.
    tags = {
        name: _tagset(f"Old {name}", tracknumber=track, discnumber=None)
        for name, track in (
            ("01-CD1.flac", "1"),
            ("01-CD2.flac", "1"),
            ("02-CD1.flac", "2"),
            ("02-CD2.flac", "2"),
        )
    }
    metadata = {
        "tracks": {
            str(disc): {str(track): _trackmeta(f"New {disc}-{track}", str(track), str(disc)) for track in (1, 2)}
            for disc in (1, 2)
        }
    }

    with pytest.raises(UploadError, match="DISCNUMBER"):
        create_track_changes(tags, metadata)


def test_tag_files_stops_on_a_flat_folder_without_disc_tags_instead_of_crashing(capsys):
    # The same layout through tag_files: it prints the refusal and returns, so the upload goes on.
    tags = {
        name: _tagset(f"Old {name}", tracknumber=track, discnumber=None)
        for name, track in (
            ("01-CD1.flac", "1"),
            ("01-CD2.flac", "1"),
            ("02-CD1.flac", "2"),
            ("02-CD2.flac", "2"),
        )
    }
    metadata = {
        "title": "Some Album",
        "edition_title": None,
        "genres": [],
        "group_year": None,
        "label": None,
        "catno": None,
        "artists": [("Some Artist", "main")],
        "upc": None,
        "comment": None,
        "tracks": {
            str(disc): {str(track): _trackmeta(f"New {disc}-{track}", str(track), str(disc)) for track in (1, 2)}
            for disc in (1, 2)
        },
    }

    # The fork stops the upload: uploading the uncorrected tags would break the tracker's tagging rules.
    with pytest.raises(UploadError, match="DISCNUMBER"):
        tag_files("/unused", tags, metadata, auto_rename=True)


def test_create_track_changes_refuses_disc_folders_whose_track_counts_differ_from_the_metadata():
    # Three files under CD1, one under CD2, against metadata that expects two tracks per disc.
    # The folder split cannot be trusted to line files up with the right disc's tracks.
    tags = {
        "CD1/01.flac": _tagset("a", tracknumber="1", discnumber=None),
        "CD1/02.flac": _tagset("b", tracknumber="2", discnumber=None),
        "CD1/03.flac": _tagset("c", tracknumber="3", discnumber=None),
        "CD2/01.flac": _tagset("d", tracknumber="1", discnumber=None),
    }
    metadata = {
        "tracks": {
            str(disc): {str(track): _trackmeta(f"New {disc}-{track}", str(track), str(disc)) for track in (1, 2)}
            for disc in (1, 2)
        }
    }

    with pytest.raises(UploadError, match="DISCNUMBER"):
        create_track_changes(tags, metadata)


def test_create_track_changes_orders_a_single_disc_single_folder_with_duplicate_track_tags_by_file_name():
    # One disc, one folder, every file TRACKNUMBER=1: the fallback retags by file order, not a refusal.
    tags = {
        "01 First.flac": _tagset("Old First", tracknumber="1", discnumber=None),
        "02 Second.flac": _tagset("Old Second", tracknumber="1", discnumber=None),
        "03 Third.flac": _tagset("Old Third", tracknumber="1", discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New First", "1", "1"),
                "2": _trackmeta("New Second", "2", "1"),
                "3": _trackmeta("New Third", "3", "1"),
            }
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old First", "New First") in changes["01 First.flac"]
    assert Change("title", "Old Second", "New Second") in changes["02 Second.flac"]
    assert Change("title", "Old Third", "New Third") in changes["03 Third.flac"]


def test_create_track_changes_orders_a_single_disc_single_folder_with_no_track_tags_by_file_name():
    # No file in the folder carries a TRACKNUMBER tag at all: still not ambiguous when the file
    # names are, so this must retag by file name order rather than refuse.
    tags = {
        "01 First.flac": _tagset("Old First", tracknumber=None, discnumber=None),
        "02 Second.flac": _tagset("Old Second", tracknumber=None, discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New First", "1", "1"),
                "2": _trackmeta("New Second", "2", "1"),
            }
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old First", "New First") in changes["01 First.flac"]
    assert Change("title", "Old Second", "New Second") in changes["02 Second.flac"]


def test_create_track_changes_orders_by_file_name_when_a_name_holds_a_digit_int_cannot_parse():
    # A superscript "2" beside digits ("01²2.flac") must not raise out of the file-name fallback.
    tags = {
        "01²2.flac": _tagset("Old First", tracknumber=None, discnumber=None),
        "02.flac": _tagset("Old Second", tracknumber=None, discnumber=None),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": _trackmeta("New First", "1", "1"),
                "2": _trackmeta("New Second", "2", "1"),
            }
        }
    }

    changes = create_track_changes(tags, metadata)

    assert Change("title", "Old First", "New First") in changes["01²2.flac"]
    assert Change("title", "Old Second", "New Second") in changes["02.flac"]


def test_tag_files_stops_when_one_disc_folder_is_genuinely_ambiguous(capsys):
    # CD1's files have no track tags and tie on file name too: tag_files prints the refusal
    # and returns, so the upload goes on with the current tags.
    tags = {
        "CD1/Track.flac": _tagset("Old A", tracknumber=None, discnumber=None),
        "CD1/track.flac": _tagset("Old B", tracknumber=None, discnumber=None),
        "CD2/01.flac": _tagset("Old CD2 1", tracknumber="1", discnumber=None),
    }
    metadata = {
        "title": "Some Album",
        "edition_title": None,
        "genres": [],
        "group_year": None,
        "label": None,
        "catno": None,
        "artists": [("Some Artist", "main")],
        "upc": None,
        "comment": None,
        "tracks": {
            "1": {
                "1": _trackmeta("New CD1 1", "1", "1"),
                "2": _trackmeta("New CD1 2", "2", "1"),
            },
            "2": {
                "1": _trackmeta("New CD2 1", "1", "2"),
            },
        },
    }

    # The fork stops the upload: uploading the uncorrected tags would break the tracker's tagging rules.
    with pytest.raises(UploadError, match="DISCNUMBER"):
        tag_files("/unused", tags, metadata, auto_rename=True)


def test_create_track_changes_handles_the_one_folder_disc_dot_track_layout():
    # #479's flat "<disc>.<track> ..." names already carry real disc/track tags: the tag-sorted path.
    tags = {
        "2.01 Third.flac": _tagset("Old Third", tracknumber="1", discnumber="2"),
        "1.01 First.flac": _tagset("Old First", tracknumber="1", discnumber="1"),
        "1.02 Second.flac": _tagset("Old Second", tracknumber="2", discnumber="1"),
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

    assert Change("title", "Old First", "New First") in changes["1.01 First.flac"]
    assert Change("title", "Old Second", "New Second") in changes["1.02 Second.flac"]
    assert Change("title", "Old Third", "New Third") in changes["2.01 Third.flac"]


def test_get_tag_number_defaults_a_digit_like_value_int_cannot_parse():
    # "²" (superscript two) passes str.isdigit() but int() rejects it; a malformed
    # TRACKNUMBER like this must read as unparseable rather than raise out of retagging.
    assert _get_tag_number({"tracknumber": ["²"]}, "tracknumber") == 1


def test_a_track_count_mismatch_stops_the_retag():
    # zip would otherwise drop the extra file without a word, retagging the rest one track off.
    tags = {f"0{n}.flac": _tagset(f"Old {n}", tracknumber=str(n), discnumber=None) for n in (1, 2, 3)}
    metadata = {"tracks": {"1": {str(n): _trackmeta(f"New {n}", str(n), "1") for n in (1, 2)}}}
    with pytest.raises(UploadError, match="Track count mismatch"):
        create_track_changes(tags, metadata)


def test_disc_tags_order_a_flat_folder_when_the_metadata_has_one_disc():
    files = {
        "01 Alpha.flac": ("1", "1"),
        "02 Beta.flac": ("1", "2"),
        "01 Gamma.flac": ("2", "1"),
        "02 Delta.flac": ("2", "2"),
    }
    tags = {name: _tagset(f"Old {name}", tracknumber=track, discnumber=disc) for name, (disc, track) in files.items()}
    metadata = {"tracks": {"1": {str(n): _trackmeta(f"New {n}", str(n), "1") for n in range(1, 5)}}}

    changes = create_track_changes(tags, metadata)

    for n, name in enumerate(files, 1):
        assert Change("title", f"Old {name}", f"New {n}") in changes[name]


def test_tracks_numbered_through_one_tagged_disc_fill_the_metadata_discs():
    tags = {f"0{n}.flac": _tagset(f"Old {n}", tracknumber=str(n), discnumber="1") for n in range(1, 5)}

    changes = create_track_changes(tags, _two_discs_of_two())

    assert Change("title", "Old 3", "New 2-1") in changes["03.flac"]
    assert Change("title", "Old 4", "New 2-2") in changes["04.flac"]


def test_names_without_a_number_are_not_an_order():
    tags = {name: _tagset(f"Old {name}", tracknumber="1", discnumber=None) for name in ("Zebra.flac", "Apple.flac")}
    metadata = {"tracks": {"1": {"1": _trackmeta("New 1", "1", "1"), "2": _trackmeta("New 2", "2", "1")}}}

    with pytest.raises(UploadError, match="DISCNUMBER and TRACKNUMBER"):
        create_track_changes(tags, metadata)
