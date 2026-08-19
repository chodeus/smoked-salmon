from salmon.checks.tag_rules import collect_upload_warnings


def _track(sample_rate=44100, precision=16):
    return {"sample rate": sample_rate, "precision": precision}


def test_no_warnings_for_standard_upload():
    tracks = {"01. Song.flac": _track()}
    assert collect_upload_warnings("RED", "Artist - Album (2020) [WEB FLAC]", tracks) == []


def test_red_path_length_flagged():
    warnings = collect_upload_warnings("RED", "A" * 170, {"01. Song.flac": _track()})
    assert len(warnings) == 1
    assert "exceeds RED's 180" in warnings[0]


def test_ops_allows_longer_path_than_red():
    # 170-char folder → ~184-char path: over RED's 180 but under OPS's 255.
    assert collect_upload_warnings("OPS", "A" * 170, {"01. Song.flac": _track()}) == []


def test_ops_path_over_255_flagged():
    warnings = collect_upload_warnings("OPS", "A" * 260, {"01. Song.flac": _track()})
    assert len(warnings) == 1
    assert "exceeds OPS's 255" in warnings[0]


def test_nonstandard_sample_rate_flagged():
    warnings = collect_upload_warnings("OPS", "F", {"a.flac": _track(sample_rate=44056)})
    assert len(warnings) == 1
    assert "44056" in warnings[0]


def test_16bit_above_48k_red_is_trumpable():
    warnings = collect_upload_warnings("RED", "F", {"a.flac": _track(sample_rate=96000, precision=16)})
    assert len(warnings) == 1
    assert "trumpable" in warnings[0]


def test_16bit_above_48k_ops_not_permitted():
    warnings = collect_upload_warnings("OPS", "F", {"a.flac": _track(sample_rate=96000, precision=16)})
    assert len(warnings) == 1
    assert "not permitted on OPS" in warnings[0]


def test_24bit_96k_is_fine():
    assert collect_upload_warnings("RED", "F", {"a.flac": _track(sample_rate=96000, precision=24)}) == []
