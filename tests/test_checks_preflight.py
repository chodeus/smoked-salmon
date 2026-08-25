import json

import pytest

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


@pytest.mark.parametrize("source", ["WEB", "Vinyl", "Cassette"])
def test_log_check_does_not_apply_to_non_cd(source):
    assert pf._log_verdict({"logs": []}, {"source": source})[0] == pf.SKIP


def test_missing_log_on_cd_warns_but_does_not_block():
    row = pf._log_verdict({"logs": []}, {"source": "CD"})
    assert row[0] == pf.WARN


def test_unknown_source_still_checks_the_log():
    assert pf._log_verdict({"logs": []}, {"source": None})[0] == pf.WARN


def test_perfect_log_is_green():
    logs = {"logs": [{"file": "r.log", "score": 100, "checksum_integrity": "Match"}]}
    assert pf._log_verdict(logs, {"source": "CD"})[0] == pf.OK


def test_imperfect_score_warns():
    logs = {"logs": [{"file": "r.log", "score": 87, "checksum_integrity": "Match"}]}
    row = pf._log_verdict(logs, {"source": "CD"})
    assert row[0] == pf.WARN
    assert "87" in row[1]


def test_checksum_mismatch_warns_even_at_full_score():
    logs = {"logs": [{"file": "r.log", "score": 100, "checksum_integrity": "Mismatch"}]}
    assert pf._log_verdict(logs, {"source": "CD"})[0] == pf.WARN


def test_worst_log_decides_the_verdict():
    logs = {
        "logs": [
            {"file": "a.log", "score": 100, "checksum_integrity": "Match"},
            {"file": "b.log", "score": 40, "checksum_integrity": "Match"},
        ]
    }
    row = pf._log_verdict(logs, {"source": "CD"})
    assert row[0] == pf.WARN
    assert "b.log" in row[1]


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


def test_no_dupes_is_green():
    assert pf.dupe_row("OPS", []).verdict == pf.OK


def test_blacklisted_release_blocks():
    assert pf.blacklist_row("RED", "on the Do-Not-Upload list").verdict == pf.BLOCK


async def test_run_checks_blocks_until_a_source_is_chosen(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": None, "confidence": "unknown", "reasons": ["undecidable"]}
    )
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, None, [])
    assert result["blocking"] == ["source"]
    assert [r["verdict"] for r in result["rows"] if r["id"] != "source"] == [pf.SKIP] * 5


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
    class FakeSite:
        base_url = "https://redacted.sh"

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


async def test_a_failing_blacklist_lookup_blocks_rather_than_clears(album_dir, monkeypatch):
    """Fail closed: an unreadable blacklist must never look like 'not blacklisted'."""
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(
        pf, "_release_identity", lambda _p: {"artists": [("X", "main")], "title": "Y", "label": None, "catno": None}
    )
    monkeypatch.setattr(pf, "get_search_results", lambda *_a: _empty())
    monkeypatch.setattr(pf, "red_blacklist_reason", lambda *_a: (_ for _ in ()).throw(RuntimeError("bad toml")))
    result = await pf.run_checks(str(album_dir), NO_FILE_CHECKS, "WEB", ["RED"])
    assert "blacklist:RED" in result["blocking"]


async def _empty():
    return []


def test_one_unparseable_log_warns_even_when_another_scores_full_marks():
    """A good log does not vouch for a sibling that would not parse."""
    logs = {
        "logs": [
            {"file": "good.log", "score": 100, "checksum_integrity": "Match"},
            {"file": "broken.log", "error": "not a log"},
        ]
    }
    verdict, detail = pf._log_verdict(logs, {"source": "CD"})
    assert verdict == pf.WARN
    assert "1 of 2" in detail


def test_a_log_with_no_checksum_is_unverifiable_not_altered():
    # EAC only began signing logs in 1.0 beta 3; an older log has none at all,
    # which is a different state from a checksum that fails.
    logs = {"logs": [{"file": "r.log", "score": 100, "checksum_integrity": "Unknown"}]}
    verdict, detail = pf._log_verdict(logs, {"source": "CD"})
    assert verdict == pf.WARN
    assert "No log checksum" in detail
    assert "does not match" not in detail
    assert "trumpable" in detail


def test_a_failing_checksum_still_says_the_log_was_altered():
    logs = {"logs": [{"file": "r.log", "score": 100, "checksum_integrity": "Mismatch"}]}
    verdict, detail = pf._log_verdict(logs, {"source": "CD"})
    assert verdict == pf.WARN
    assert "does not match" in detail
    assert "altered" in detail


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


def test_rules_row_is_green_when_nothing_breaks_a_rule():
    row = pf.rules_row("RED", "Folder", {"01.flac": {"sample rate": 44100, "precision": 16}})
    assert row.verdict == pf.OK


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
