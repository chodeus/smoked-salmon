from asyncclick.testing import CliRunner

from salmon.checks import all_checks


async def _run(album_dir, *args):
    return await CliRunner().invoke(all_checks, [str(album_dir), *args])


async def test_prints_a_row_for_every_check(album_dir, monkeypatch):
    from salmon.checks import preflight as pf

    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    result = await _run(album_dir, "--source", "WEB")
    for label in ("Source", "File integrity", "Upconvert", "MQA", "Rip log"):
        assert label in result.output


async def test_exits_non_zero_when_a_release_is_unfit(album_dir, monkeypatch):
    from salmon.checks import preflight as pf

    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    result = await _run(album_dir, "--source", "WEB")
    # the fixture's dummy audio cannot decode, so integrity blocks
    assert result.exit_code == 1
    assert "Not suitable for upload" in result.output


async def test_an_undetermined_source_alone_does_not_fail_the_command(album_dir, monkeypatch):
    """This is a diagnostic: nothing is being claimed, so an unknown source is not a defect."""
    from salmon.checks import preflight as pf

    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": None, "confidence": "unknown", "reasons": ["undecidable"]}
    )
    monkeypatch.setattr(pf, "CHECKS", ())  # isolate the source row from the file checks
    result = await _run(album_dir)
    assert result.exit_code == 0
    assert "Source" in result.output


async def test_unknown_tracker_is_rejected(album_dir):
    result = await _run(album_dir, "--tracker", "NOPE")
    assert result.exit_code != 0
    assert "Unknown tracker" in result.output


async def test_invalid_source_is_rejected(album_dir):
    """An unknown source is truthy, so without this it would silently skip the log check."""
    result = await _run(album_dir, "--source", "INVALID")
    assert result.exit_code != 0
    assert "Unknown source" in result.output


async def test_without_a_tracker_red_and_ops_rules_apply_and_none_is_contacted(album_dir, monkeypatch):
    from salmon.checks import preflight as pf

    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(pf, "CHECKS", ())
    identity = {"artists": [("X", "main")], "title": "Y", "edition_title": None, "year": None, "catno": None}
    monkeypatch.setattr(pf, "_release_identity", lambda _p: identity | {"label": None})
    contacted: list[str] = []
    monkeypatch.setattr(pf.salmon.trackers, "get_class", contacted.append)

    result = await _run(album_dir, "--source", "WEB")

    for label in ("RED rules", "OPS rules", "Do-Not-Upload (RED)", "Do-Not-Upload (OPS)"):
        assert label in result.output
    assert "Duplicate" not in result.output
    assert contacted == []


async def test_each_row_prints_its_notes_under_it(album_dir, monkeypatch):
    from salmon.checks import preflight as pf

    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(pf, "CHECKS", ())
    monkeypatch.setattr(pf, "_release_identity", lambda _p: {})
    long_log = "x" * 180 + ".log"
    (album_dir / long_log).write_text("log", encoding="utf-8")

    result = await _run(album_dir, "--source", "WEB")

    note_lines = [line for line in result.output.splitlines() if line.lstrip().startswith("- ")]
    assert any(long_log in line for line in note_lines)
