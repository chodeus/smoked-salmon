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
