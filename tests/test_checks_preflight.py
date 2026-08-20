import pytest

from salmon.checks import preflight as pf

ALL_SKIPPED: dict[str, bool] = dict.fromkeys(["log", "integrity", "mqa", "upconvert"], True)


def test_integrity_failure_blocks():
    row = pf.integrity_row({"passed": False, "details": "track 3 failed to decode"})
    assert row["verdict"] == pf.BLOCK
    assert "track 3" in row["detail"]


def test_integrity_pass_is_green():
    assert pf.integrity_row({"passed": True, "details": ""})["verdict"] == pf.OK


def test_mqa_detection_blocks():
    result = {"detected": True, "files": [{"file": "01.flac", "detected": True}]}
    assert pf.mqa_row(result)["verdict"] == pf.BLOCK


def test_upconvert_blocks_and_names_the_file():
    result = {"files": [{"file": "01.flac", "is_upconverted": True}, {"file": "02.flac", "is_upconverted": False}]}
    row = pf.upconvert_row(result)
    assert row["verdict"] == pf.BLOCK
    assert "01.flac" in row["detail"]


def test_upconvert_errors_warn_rather_than_pass():
    row = pf.upconvert_row({"files": [{"file": "01.flac", "error": "boom"}]})
    assert row["verdict"] == pf.WARN


def test_upconvert_with_no_flacs_is_skipped():
    assert pf.upconvert_row({"files": []})["verdict"] == pf.SKIP


@pytest.mark.parametrize("source", ["WEB", "Vinyl", "Cassette"])
def test_log_check_does_not_apply_to_non_cd(source):
    assert pf.log_row({"logs": []}, source)["verdict"] == pf.SKIP


def test_missing_log_on_cd_warns_but_does_not_block():
    row = pf.log_row({"logs": []}, "CD")
    assert row["verdict"] == pf.WARN


def test_unknown_source_still_checks_the_log():
    assert pf.log_row({"logs": []}, None)["verdict"] == pf.WARN


def test_perfect_log_is_green():
    logs = {"logs": [{"file": "r.log", "score": 100, "checksum_integrity": "Match"}]}
    assert pf.log_row(logs, "CD")["verdict"] == pf.OK


def test_imperfect_score_warns():
    logs = {"logs": [{"file": "r.log", "score": 87, "checksum_integrity": "Match"}]}
    row = pf.log_row(logs, "CD")
    assert row["verdict"] == pf.WARN
    assert "87" in row["detail"]


def test_checksum_mismatch_warns_even_at_full_score():
    logs = {"logs": [{"file": "r.log", "score": 100, "checksum_integrity": "Mismatch"}]}
    assert pf.log_row(logs, "CD")["verdict"] == pf.WARN


def test_worst_log_decides_the_verdict():
    logs = {
        "logs": [
            {"file": "a.log", "score": 100, "checksum_integrity": "Match"},
            {"file": "b.log", "score": 40, "checksum_integrity": "Match"},
        ]
    }
    row = pf.log_row(logs, "CD")
    assert row["verdict"] == pf.WARN
    assert "b.log" in row["detail"]


def test_source_must_be_chosen_when_undetectable():
    guess = {"source": None, "confidence": "unknown", "reasons": ["no evidence"]}
    assert pf.source_row(guess, None)["verdict"] == pf.BLOCK


def test_detected_source_still_needs_picking():
    guess = {"source": "CD", "confidence": "confirmed", "reasons": ["EAC log"]}
    assert pf.source_row(guess, None)["verdict"] == pf.BLOCK


def test_source_conflicting_with_evidence_warns():
    guess = {"source": "CD", "confidence": "confirmed", "reasons": ["EAC log"]}
    row = pf.source_row(guess, "WEB")
    assert row["verdict"] == pf.WARN
    assert "CD" in row["detail"]


def test_unverifiable_source_warns_when_picked():
    guess = {"source": None, "confidence": "unknown", "reasons": ["no evidence"]}
    assert pf.source_row(guess, "WEB")["verdict"] == pf.WARN


def test_matching_source_is_green():
    guess = {"source": "CD", "confidence": "confirmed", "reasons": ["EAC log"]}
    assert pf.source_row(guess, "CD")["verdict"] == pf.OK


def test_dupe_hits_warn():
    row = pf.dupe_row("RED", [{"groupId": 1, "groupName": "In Rainbows"}])
    assert row["verdict"] == pf.WARN
    assert "In Rainbows" in row["detail"]


def test_no_dupes_is_green():
    assert pf.dupe_row("OPS", [])["verdict"] == pf.OK


def test_blacklisted_release_blocks():
    assert pf.blacklist_row("RED", "on the Do-Not-Upload list")["verdict"] == pf.BLOCK


async def test_run_preflight_blocks_until_a_source_is_chosen(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": None, "confidence": "unknown", "reasons": ["undecidable"]}
    )
    result = await pf.run_preflight(str(album_dir), None, [], ALL_SKIPPED)
    assert result["blocking"] == ["source"]
    assert [r["verdict"] for r in result["rows"] if r["id"] != "source"] == [pf.SKIP] * 4


async def test_run_preflight_clears_when_everything_passes(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    result = await pf.run_preflight(str(album_dir), "WEB", [], ALL_SKIPPED)
    assert result["blocking"] == []
    assert result["warnings"] == []


async def test_run_preflight_reports_a_tracker_search_failure_as_a_warning(album_dir, monkeypatch):
    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(
        pf, "_release_identity", lambda _p: {"artists": [("X", "main")], "title": "Y", "label": None, "catno": None}
    )

    def _boom(_code):
        raise RuntimeError("tracker down")

    monkeypatch.setattr(pf.salmon.trackers, "get_class", _boom)
    result = await pf.run_preflight(str(album_dir), "WEB", ["RED"], ALL_SKIPPED)
    assert "dupe:RED" in result["warnings"]
    assert result["blocking"] == []
