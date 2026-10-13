import json
from types import SimpleNamespace

import pytest
from test_checks_do_not_upload import write_lists  # pyright: ignore[reportMissingImports]

from salmon.checks import album
from salmon.checks import preflight as pf

NO_FILE_CHECKS: list[str] = []


def test_integrity_failure_blocks():
    row = pf._integrity_verdict({"passed": False, "details": "track 3 failed to decode"}, {})
    assert row[0] == pf.BLOCK
    assert "track 3" in row[1]


def test_integrity_pass_is_green():
    assert pf._integrity_verdict({"passed": True, "details": ""}, {})[0] == pf.OK


def test_mqa_detection_blocks():
    result = {"detected": True, "files": [{"file": "01.flac", "detected": True}]}
    assert pf._mqa_verdict(result, {})[0] == pf.BLOCK


def test_upconvert_blocks_and_names_the_file():
    result = {"files": [{"file": "01.flac", "is_upconverted": True}, {"file": "02.flac", "is_upconverted": False}]}
    row = pf._upconvert_verdict(result, {})
    assert row[0] == pf.BLOCK
    assert "01.flac" in row[1]


def test_upconvert_errors_warn_rather_than_pass():
    row = pf._upconvert_verdict({"files": [{"file": "01.flac", "error": "boom"}]}, {})
    assert row[0] == pf.WARN


def test_upconvert_with_no_flacs_is_skipped():
    assert pf._upconvert_verdict({"files": []}, {})[0] == pf.SKIP


def test_upconvert_16bit_is_out_of_scope_rather_than_a_failed_test():
    result = {"files": [{"file": f"0{i}.flac", "not_applicable": "This is a 16bit FLAC file."} for i in (1, 2)]}
    row = pf._upconvert_verdict(result, {})
    assert row[0] == pf.SKIP
    assert "16bit" in row[1]


def test_upconvert_warning_names_the_reason_and_counts_only_testable_files():
    result = {
        "files": [
            {"file": "01.flac", "not_applicable": "This is a 16bit FLAC file."},
            {"file": "02.flac", "error": "File appears to be corrupt: bad frame"},
        ]
    }
    row = pf._upconvert_verdict(result, {})
    assert row[0] == pf.WARN
    assert "1 of 1" in row[1]
    assert "bad frame" in row[1]


def test_upconvert_still_blocks_when_a_deeper_file_is_upconverted():
    result = {
        "files": [
            {"file": "01.flac", "not_applicable": "This is a 16bit FLAC file."},
            {"file": "02.flac", "is_upconverted": True},
        ]
    }
    assert pf._upconvert_verdict(result, {})[0] == pf.BLOCK


def _log(**fields) -> dict:
    """A rip log as album.run_log_check reads it: full marks, unless fields say otherwise."""
    return {"file": "rip.log", "score": 100, "checksum_integrity": "Match", "crcs": "match", "crcs_detail": ""} | fields


def test_a_missing_log_warns_but_does_not_block():
    verdict, _detail = pf._log_verdict({"logs": []}, {})
    assert verdict == pf.WARN


def test_a_full_marks_log_is_green():
    found = pf._log_verdict({"logs": [_log()]}, {})
    assert found == (pf.OK, "Score 100/100, checksum and CRCs match in 1 log(s).")


@pytest.mark.parametrize(
    ("fields", "verdict", "said"),
    [
        ({"checksum_integrity": "Mismatch"}, pf.BLOCK, "rip.log: its checksum does not match, so the log was edited."),
        (
            {"crcs": "error", "crcs_detail": "no decoder"},
            pf.BLOCK,
            "could not be checked against its CRCs (no decoder)",
        ),
        ({"crcs": "mismatch"}, pf.WARN, "rip.log: the audio does not match its CRCs."),
        (
            {"crcs": "skipped", "crcs_detail": "No audio files found!"},
            pf.WARN,
            "CRCs not checked: No audio files found!",
        ),
        ({"checksum_integrity": "Unknown"}, pf.WARN, "no checksum to verify it"),
        ({"score": 87}, pf.WARN, "rip.log: score 87/100."),
        ({"score": None}, pf.WARN, "rip.log: no score."),
    ],
    ids=["edited", "crcs-unverifiable", "crc-mismatch", "crcs-skipped", "no-checksum", "low-score", "no-score"],
)
def test_each_log_problem_gets_the_verdict_salmon_up_gives_it(fields: dict, verdict: str, said: str):
    found, detail = pf._log_verdict({"logs": [_log(**fields)]}, {})
    assert found == verdict
    assert said in detail


def test_a_good_log_does_not_vouch_for_one_that_cannot_be_read():
    logs = {"logs": [_log(file="good.log"), {"file": "broken.log", "error": "not a log"}]}
    found = pf._log_verdict(logs, {})
    assert found == (pf.WARN, "broken.log: could not be read (not a log). 1 other log(s) pass.")


def test_one_edited_log_blocks_whatever_the_others_say():
    logs = [_log(file="cd1.log", score=90), _log(file="cd2.log", checksum_integrity="Mismatch"), _log(file="cd3.log")]
    found = pf._log_verdict({"logs": logs}, {})
    assert found == (
        pf.BLOCK,
        "cd1.log: score 90/100. cd2.log: its checksum does not match, so the log was edited. 1 other log(s) pass.",
    )


@pytest.mark.parametrize(("source", "read"), [("WEB", False), ("Vinyl", False), ("CD", True), (None, True)])
async def test_logs_are_read_for_a_cd_or_an_unknown_source_only(album_dir, monkeypatch, source, read: bool):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": None, "confidence": "unknown", "reasons": ["undecidable"]}
    )
    searched: list[str] = []
    monkeypatch.setattr(album, "_log_paths", lambda path: searched.append(path) or [])

    result = await pf.run_checks(str(album_dir), ["log"], source, [])

    row = next(r for r in result["rows"] if r["id"] == "log")
    assert bool(searched) is read
    assert row["verdict"] == (pf.WARN if read else pf.SKIP)


def test_source_must_be_chosen_when_undetectable():
    guess = {"source": None, "confidence": "unknown", "reasons": ["no evidence"]}
    assert pf.source_row(guess, None).verdict == pf.BLOCK


def test_detected_source_still_needs_picking():
    guess = {"source": "CD", "confidence": "confirmed", "reasons": ["EAC log"]}
    assert pf.source_row(guess, None).verdict == pf.BLOCK


def test_source_conflicting_with_evidence_warns():
    guess = {"source": "CD", "confidence": "confirmed", "reasons": ["EAC log"]}
    row = pf.source_row(guess, "WEB")
    assert row.verdict == pf.WARN
    assert "CD" in row.detail


def test_unverifiable_source_warns_when_picked():
    guess = {"source": None, "confidence": "unknown", "reasons": ["no evidence"]}
    assert pf.source_row(guess, "WEB").verdict == pf.WARN


def test_matching_source_is_green():
    guess = {"source": "CD", "confidence": "confirmed", "reasons": ["EAC log"]}
    assert pf.source_row(guess, "CD").verdict == pf.OK


def test_dupe_hits_warn():
    row = pf.dupe_row("RED", [{"groupId": 1, "groupName": "In Rainbows"}])
    assert row.verdict == pf.WARN
    assert "In Rainbows" in row.detail


ORACLE_GROUP = {
    "groupId": 2846669,
    "groupName": "Oracle (feat. Jem Cooke)",
    "groupYear": 2026,
    "torrents": [
        {
            "media": "WEB",
            "format": "FLAC",
            "encoding": "Lossless",
            "remasterYear": 2026,
            "remasterCatalogueNumber": "1200214726676",
        },
        {
            "media": "WEB",
            "format": "MP3",
            "encoding": "320",
            "remasterYear": 2026,
            "remasterCatalogueNumber": "1200214726676",
        },
    ],
}


def test_dupe_row_names_each_matched_edition():
    """The catalogue number shows a 1-track single and a 3-track release apart without opening the site."""
    row = pf.dupe_row("RED", [ORACLE_GROUP])

    assert "2026 / 1200214726676: WEB FLAC Lossless, WEB MP3 320" in row.detail


def _oracle_release(**fields) -> dict:
    """The release ORACLE_GROUP's FLAC is, unless fields say otherwise."""
    return {"source": "WEB", "format": "FLAC", "encoding": "Lossless", "year": "2026", "edition_title": None} | fields


@pytest.mark.parametrize(
    ("fields", "verdict", "said"),
    [
        (
            {},
            pf.WARN,
            "RED already has this edition's WEB FLAC Lossless: 2026 / 1200214726676 / WEB / FLAC / Lossless.",
        ),
        ({"source": "CD"}, pf.OK, "None holds this edition's CD FLAC Lossless."),
        ({"encoding": "24bit Lossless"}, pf.OK, "None holds this edition's WEB FLAC 24bit Lossless."),
        ({"year": "2020"}, pf.OK, "None holds this edition's WEB FLAC Lossless."),
        ({"edition_title": "Deluxe Edition"}, pf.WARN, "RED already has this edition's WEB FLAC Lossless"),
        ({"encoding": None}, pf.WARN, "not known from the files, so check them by hand"),
    ],
    ids=["same-edition", "other-media", "other-encoding", "other-year", "untitled-held-edition", "unknown-encoding"],
)
def test_a_match_warns_only_when_it_holds_this_editions_format(fields: dict, verdict: str, said: str):
    row = pf.dupe_row("RED", [ORACLE_GROUP], _oracle_release(**fields))

    assert row.verdict == verdict
    assert said in row.detail


@pytest.mark.parametrize(
    ("track_data", "release_format", "encoding"),
    [
        ({"01.flac": {"precision": 24}, "02.flac": {"precision": 16}}, "FLAC", "24bit Lossless"),
        ({"01.flac": {"precision": 16}}, "FLAC", "Lossless"),
        ({"01.mp3": {"precision": 16}}, "MP3", None),
        ({"01.xyz": {}}, None, None),
        ({}, None, None),
    ],
    ids=["24bit", "16bit", "mp3", "unknown-extension", "unread"],
)
def test_the_release_takes_its_format_and_encoding_from_the_files(track_data, release_format, encoding):
    release = pf._release({"title": "Y"}, track_data, "WEB")

    assert (release["format"], release["encoding"], release["source"]) == (release_format, encoding, "WEB")


async def _offline(_site, _searchstrs):
    raise RuntimeError("offline")


async def _null_group(_site, _searchstrs):
    return [None]


async def _no_results_field(_site, _searchstrs):
    raise KeyError("results")


@pytest.mark.parametrize(
    ("search", "said"),
    [
        (_offline, "Could not search DIC: offline"),
        (_null_group, "DIC gave an unexpected answer"),
        (_no_results_field, "DIC gave an unexpected answer"),
    ],
    ids=["offline", "null-group", "no-results-field"],
)
async def test_a_failed_or_malformed_search_warns_and_closes_the_client(monkeypatch, search, said: str):
    closed: list[bool] = []

    class Site:
        base_url = "https://tracker.example"

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(pf.salmon.trackers, "get_class", lambda _code: Site)
    monkeypatch.setattr(pf, "get_search_results", search)
    release = _oracle_release(artists=[("X", "main")], title="Y", catno=None)

    rows, raw = await pf._tracker_rows("DIC", release)

    assert [(row.verdict, said in row.detail) for row in rows] == [(pf.WARN, True)]
    assert raw == {}
    assert closed == [True]


@pytest.mark.parametrize(("source", "verdict"), [("WEB", pf.WARN), ("CD", pf.OK)])
async def test_run_checks_weighs_the_matches_against_the_chosen_source(album_dir, monkeypatch, source, verdict):
    class Site:
        base_url = "https://tracker.example"

        async def close(self) -> None:
            pass

    async def oracle(_site, _searchstrs):
        return [ORACLE_GROUP]

    monkeypatch.setattr(pf, "detect_source", lambda _p: {"source": None, "confidence": "unknown", "reasons": ["none"]})
    identity = {"artists": [("X", "main")], "title": "Oracle", "edition_title": None, "year": "2026", "catno": None}
    monkeypatch.setattr(pf, "_release_identity", lambda _p: identity | {"label": None})
    monkeypatch.setattr(pf, "_audio_info", lambda _p: {"01.flac": {"precision": 16, "sample rate": 44100}})
    monkeypatch.setattr(pf.salmon.trackers, "get_class", lambda _code: Site)
    monkeypatch.setattr(pf, "get_search_results", oracle)

    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, source, ["DIC"])

    row = next(r for r in result["rows"] if r["id"] == "dupe:DIC")
    assert row["verdict"] == verdict


def test_dupe_matches_keep_the_catalogue_number():
    [group] = pf.dupe_matches("https://redacted.sh", [ORACLE_GROUP])

    assert group["torrents"][0]["remasterCatalogueNumber"] == "1200214726676"


def test_dupe_matches_give_an_original_release_the_group_year():
    """The table shows the same year as the summary row, not a blank or a 0."""
    original = {"groupId": 1, "groupYear": 2008, "torrents": [{"remasterYear": 0}, {}, {"remasterYear": 2016}]}
    [group] = pf.dupe_matches("https://redacted.sh", [original])

    years = [t["remasterYear"] for t in group["torrents"]]
    assert years == [2008, 2008, 2016]


def test_no_dupes_is_green():
    assert pf.dupe_row("OPS", []).verdict == pf.OK


def test_a_listed_release_blocks():
    assert pf.do_not_upload_row("RED", "on the Do-Not-Upload list").verdict == pf.BLOCK


async def test_run_checks_blocks_until_a_source_is_chosen(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": None, "confidence": "unknown", "reasons": ["undecidable"]}
    )
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, None, [])
    assert result["blocking"] == ["source"]
    assert [r["verdict"] for r in result["rows"] if r["id"] != "source"] == [pf.SKIP] * len(pf.CHECKS)


async def test_run_checks_clears_when_everything_passes(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", [])
    assert result["blocking"] == []
    assert result["warnings"] == []


async def test_run_checks_reports_a_tracker_search_failure_as_a_warning(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(
        pf, "_release_identity", lambda _p: {"artists": [("X", "main")], "title": "Y", "label": None, "catno": None}
    )

    def _boom(_code):
        raise RuntimeError("tracker down")

    monkeypatch.setattr(pf.salmon.trackers, "get_class", _boom)
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED"])
    assert "dupe:RED" in result["warnings"]
    assert result["blocking"] == []


async def test_run_checks_exposes_every_dupe_match_not_just_the_two_it_names(album_dir, monkeypatch):
    closed: list[bool] = []

    class FakeSite:
        base_url = "https://redacted.sh"

        async def close(self) -> None:
            closed.append(True)

    async def three_matches(_site, _searchstrs):
        return [
            {
                "groupId": 42,
                "groupName": "Album",
                "artist": "X",
                "groupYear": 2008,
                "releaseType": "Single",
                "downloadUrl": "https://redacted.sh/action=download&torrent_pass=SECRET",
                "torrents": [
                    {"torrentId": 7, "format": "FLAC", "encoding": "Lossless", "media": "CD", "logScore": 100},
                    {"torrentId": 8, "format": "MP3", "encoding": "320", "media": "WEB", "seeders": 0},
                ],
            },
            {"groupId": 43, "groupName": "Second", "torrents": []},
            {"groupId": 44, "groupName": "Third", "torrents": []},
        ]

    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(
        pf, "_release_identity", lambda _p: {"artists": [("X", "main")], "title": "Y", "label": None, "catno": None}
    )
    monkeypatch.setattr(pf.salmon.trackers, "get_class", lambda _code: FakeSite)
    monkeypatch.setattr(pf, "get_search_results", three_matches)

    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED"])

    detail = result["raw"]["dupe:RED"]
    assert len(detail["matches"]) == 3, "the row names two; the raw payload must carry them all"
    first = detail["matches"][0]
    assert first["url"] == "https://redacted.sh/torrents.php?id=42"
    assert first["torrents"][0]["logScore"] == 100
    assert first["torrents"][1]["seeders"] == 0
    # An allowlist, so a download URL carrying the torrent pass cannot ride along.
    assert "SECRET" not in json.dumps(detail)
    assert closed == [True]


def test_every_check_is_declared_once_in_the_table():
    ids = [spec.id for spec in pf.CHECKS]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(pf.CHECK_IDS)


async def test_rows_take_their_labels_from_the_table(album_dir, monkeypatch):
    """Labels live only in CHECKS, so a rename cannot leave a stale copy behind."""
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", [])
    by_id = {r["id"]: r["label"] for r in result["rows"]}
    for spec in pf.CHECKS:
        assert by_id[spec.id] == spec.label


async def test_unselected_checks_are_skipped_not_dropped(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    result = await pf.run_checks(str(album_dir), ["mqa"], "WEB", [])
    verdicts = {r["id"]: r["verdict"] for r in result["rows"]}
    assert verdicts["integrity"] == pf.SKIP
    assert verdicts["log"] == pf.SKIP
    assert verdicts["mqa"] != pf.SKIP


def _tagged_x_y(monkeypatch) -> None:
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    identity = {"artists": [("X", "main")], "title": "Y", "edition_title": None, "label": None, "catno": None}
    monkeypatch.setattr(pf, "_release_identity", lambda _p: identity)
    monkeypatch.setattr(pf, "get_search_results", lambda *_a: _empty())


async def test_an_unreadable_list_blocks_rather_than_clears(album_dir, monkeypatch, tmp_path):
    """Fail closed: a list salmon cannot read must never look like 'not listed'."""
    _tagged_x_y(monkeypatch)
    write_lists(monkeypatch, tmp_path / "lists", RED="[[entry]]\nnot toml")
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED"])
    assert "do-not-upload:RED" in result["blocking"]


async def test_a_release_on_ops_list_blocks_ops_only(album_dir, monkeypatch, tmp_path):
    _tagged_x_y(monkeypatch)
    write_lists(monkeypatch, tmp_path / "lists", OPS="[[entry]]\nartist = 'X'\nnote = 'Fakes only.'\n")
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED", "OPS", "DIC"])
    assert result["blocking"] == ["do-not-upload:OPS"]
    assert [r["id"] for r in result["rows"] if r["id"].startswith("do-not-upload")] == [
        "do-not-upload:RED",
        "do-not-upload:OPS",
    ]


async def _empty():
    return []


def test_integrity_warnings_are_not_reported_as_clean():
    result = {"passed": True, "details": "", "concerns": ["a.mp3: WARNING: file is truncated"]}
    verdict, detail = pf._integrity_verdict(result, {})
    assert verdict == pf.WARN
    assert "truncated" in detail


def test_integrity_with_nothing_to_report_is_green():
    assert pf._integrity_verdict({"passed": True, "details": "", "concerns": []}, {})[0] == pf.OK


def test_rules_row_reports_a_path_that_would_be_trumped():
    long_name = "x" * 200
    row = pf.rules_row("RED", "Folder", {f"{long_name}.flac": {"sample rate": 44100, "precision": 16}})
    assert row.verdict == pf.WARN
    assert "180" in row.detail


@pytest.mark.parametrize(
    ("tracker", "paths"),
    [("RED", "Paths are within RED's 180-character limit"), ("DIC", "DIC sets no path limit")],
)
def test_rules_row_is_green_when_nothing_breaks_a_rule(tracker: str, paths: str):
    row = pf.rules_row(tracker, "Folder", {"01.flac": {"sample rate": 44100, "precision": 16}})
    assert row.verdict == pf.OK
    assert row.detail == f"{paths}, and sample rates are standard."


LONG_LOG = "x" * 180 + ".log"


def test_a_long_log_name_breaks_the_path_rule_as_a_track_does():
    row = pf.rules_row("RED", "Folder", {"01.flac": {"sample rate": 44100, "precision": 16}}, [LONG_LOG, "cover.jpg"])

    assert row.verdict == pf.WARN
    assert row.detail == f"191-char path exceeds RED's 180 limit (2.3.12, a trump reason): Folder/{LONG_LOG}"


def test_a_long_path_is_reported_even_when_the_audio_cannot_be_read():
    row = pf.rules_row("RED", "Folder", {}, [LONG_LOG])

    assert row.verdict == pf.WARN


def test_the_other_files_are_every_file_but_the_audio(tmp_path):
    (tmp_path / "CD1").mkdir()
    for name in ("CD1/01.flac", "CD1/rip.log", "cover.jpg"):
        (tmp_path / name).write_bytes(b"")

    others = pf._other_files(str(tmp_path), {"CD1/01.flac": {}})

    assert others == ["CD1/rip.log", "cover.jpg"]


async def test_run_checks_measures_the_path_of_a_log_beside_the_audio(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "CD", "confidence": "confirmed", "reasons": ["rip log"]}
    )
    monkeypatch.setattr(pf, "_release_identity", lambda _p: {})
    (album_dir / LONG_LOG).write_text("log", encoding="utf-8")

    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "CD", ["RED"])

    row = next(r for r in result["rows"] if r["id"] == "rules:RED")
    assert row["verdict"] == pf.WARN
    assert LONG_LOG in row["detail"]


def test_rules_row_is_skipped_when_the_audio_could_not_be_read():
    # One unreadable file used to raise out of run_checks and cost every other row.
    row = pf.rules_row("RED", "Folder", {})
    assert row.verdict == pf.SKIP


def test_unreadable_audio_does_not_take_the_whole_preflight_down(tmp_path):
    broken = tmp_path / "notaudio.flac"
    broken.write_bytes(b"fLaC" + bytes(10))
    assert pf._audio_info(str(tmp_path)) == {}


async def test_run_checks_still_returns_rows_when_the_audio_will_not_parse(album_dir, monkeypatch):
    """The fixtures are dummy FLAC bytes, so this is the unreadable case end to
    end: an unparseable file used to raise straight out of run_checks."""
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(
        pf, "_release_identity", lambda _p: {"artists": [("X", "main")], "title": "Y", "label": None, "catno": None}
    )
    monkeypatch.setattr(pf, "get_search_results", lambda _site, _s: (_ for _ in ()).throw(RuntimeError("offline")))

    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED"])

    rules = next(r for r in result["rows"] if r["id"] == "rules:RED")
    assert rules["verdict"] == pf.SKIP


async def test_the_rules_row_runs_even_when_the_tags_cannot_be_read(album_dir, monkeypatch):
    # Path length depends on the filenames, not on readable tags.
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(pf, "_release_identity", lambda _p: {})

    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED"])

    assert any(r["id"] == "rules:RED" for r in result["rows"]), "no tags must not mean no rule check"


def test_a_trailing_separator_does_not_drop_the_folder_from_the_path_length():
    import os as _os

    assert _os.path.basename("/music/Album/") == "", "the case the normpath guards against"
    assert _os.path.basename(_os.path.normpath("/music/Album/")) == "Album"


def test_unset_md5_alone_can_be_acknowledged_rather_than_blocking():
    """A missing checksum is not a broken file: flac decodes it and prints "ok"."""
    from salmon.checks.preflight import WARN, _integrity_verdict

    verdict, detail = _integrity_verdict(
        {
            "passed": False,
            "details": "",
            "concerns": [],
            "md5_unset": ["a.flac"] * 17,
            "decode_failures": [],
            "checked": 17,
        },
        {},
    )

    assert verdict == WARN
    assert "17 of 17" in detail
    assert "re-encode" in detail, "the row must say the album can be fixed before uploading"


def test_a_decode_failure_beside_unset_md5s_still_blocks():
    """The fail-open shape: one broken file in an album of MD5-less ones must not
    ride their downgrade out of the blocking verdict."""
    from salmon.checks.preflight import BLOCK, _integrity_verdict

    verdict, _detail = _integrity_verdict(
        {
            "passed": False,
            "details": "",
            "concerns": [],
            "md5_unset": ["a.flac", "b.flac"],
            "decode_failures": ["c.flac"],
            "checked": 3,
        },
        {},
    )

    assert verdict == BLOCK


def test_a_part_web_album_says_how_many():
    """ "3 of 17" and "17 of 17" are different situations and must not read alike."""
    from salmon.checks.preflight import WARN, _integrity_verdict

    verdict, detail = _integrity_verdict(
        {
            "passed": False,
            "details": "",
            "concerns": [],
            "md5_unset": ["a.flac"] * 3,
            "decode_failures": [],
            "checked": 17,
        },
        {},
    )

    assert verdict == WARN
    assert "3 of 17" in detail


async def test_a_media_only_entry_blocks_once_the_source_is_known(album_dir, monkeypatch, tmp_path):
    _tagged_x_y(monkeypatch)
    write_lists(monkeypatch, tmp_path / "lists", RED="[[entry]]\nartist = 'X'\nmedia = 'WEB'\nnote = 'WEB fakes.'\n")
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED"])
    assert "do-not-upload:RED" in result["blocking"]


async def test_a_listed_release_is_not_searched_for_on_that_tracker(album_dir, monkeypatch, tmp_path):
    _tagged_x_y(monkeypatch)
    searched: list[object] = []

    async def search(site, _searchstrs):
        searched.append(site.site_code)
        return []

    monkeypatch.setattr(pf, "get_search_results", search)
    write_lists(monkeypatch, tmp_path / "lists", RED="[[entry]]\nartist = 'X'\nnote = 'Fakes only.'\n")
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED", "OPS"])
    assert searched == ["OPS"]
    verdicts = {r["id"]: r["verdict"] for r in result["rows"]}
    assert verdicts["dupe:RED"] == pf.SKIP


async def test_without_an_album_title_the_lists_are_said_not_checked(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(pf, "_release_identity", lambda _p: {})
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED", "DIC"])
    verdicts = {r["id"]: r["verdict"] for r in result["rows"]}
    assert verdicts["do-not-upload:RED"] == pf.SKIP
    assert "do-not-upload:DIC" not in verdicts


def test_the_identity_keeps_the_edition_title(monkeypatch):
    tagged = SimpleNamespace(album="Night Songs (Deluxe Edition)", label="Label", catno="CAT1", date="2020-05-01")
    monkeypatch.setattr(pf, "gather_tags", lambda _path: {"01.flac": tagged})
    monkeypatch.setattr(pf, "construct_artists_li", lambda _tags: [("Artist", "main")])
    identity = pf._release_identity("/album")
    assert (identity["title"], identity["edition_title"], identity["year"]) == ("Night Songs", "Deluxe Edition", "2020")


def test_16bit_above_48khz_blocks_for_ops_and_warns_for_red():
    files = {"01.flac": {"sample rate": 96000, "precision": 16}}
    ops, red = pf.sixteen_bit_row("OPS", files), pf.sixteen_bit_row("RED", files)
    assert ops is not None and ops.verdict == pf.BLOCK
    assert red is not None and red.verdict == pf.WARN
    dic = pf.sixteen_bit_row("DIC", files)
    twenty_four = pf.sixteen_bit_row("OPS", {"01.flac": {"sample rate": 96000, "precision": 24}})
    assert dic is None
    assert twenty_four is None
