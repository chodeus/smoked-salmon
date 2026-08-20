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
    assert [r["verdict"] for r in result["rows"] if r["id"] != "source"] == [pf.SKIP] * 4


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
