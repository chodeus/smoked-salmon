"""The fork's rule warnings: an upload's own list, and the full one salmon web's checks page shows."""

import pytest
from test_uploader_after_first_upload import _sync, _upload, flow  # noqa: F401  # pyright: ignore[reportMissingImports]

import salmon.trackers
import salmon.uploader
from salmon.checks import tag_rules
from salmon.checks.high_rate import sixteen_bit_notice
from salmon.checks.tag_rules import collect_upload_warnings, path_limit_for, path_limit_warning, upload_warnings
from salmon.constants import TAG_TRUMP_SIZE


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


@pytest.mark.parametrize(("tracker", "ending"), [("RED", "RED can trump them."), ("OPS", "OPS refuses them.")])
def test_16bit_above_48k_is_each_trackers_own_rule(tracker, ending):
    files = {"a.flac": _track(sample_rate=96000, precision=16)}
    rule = salmon.trackers.tracker_classes[tracker].TAG_RULES.sixteen_bit_above_48khz
    notice = sixteen_bit_notice(tracker, rule, files)
    assert notice == f"1 16bit file(s) above 48 kHz: a.flac (96 kHz). {ending}"


def test_the_upload_warnings_leave_16bit_to_its_refusal():
    """Said once, by the refusal or the trump warning, not again among the rule warnings."""
    warnings = collect_upload_warnings("OPS", "F", {"a.flac": _track(sample_rate=96000, precision=16)})
    assert warnings == []


def test_24bit_96k_is_fine():
    assert collect_upload_warnings("RED", "F", {"a.flac": _track(sample_rate=96000, precision=24)}) == []


@pytest.mark.parametrize(("length", "warned"), [(180, False), (181, True)])
def test_a_path_of_the_limits_length_is_within_it(length: int, warned: bool) -> None:
    warning = path_limit_warning("RED", "F/" + "x" * (length - 2))

    assert (warning is not None) is warned


@pytest.mark.parametrize(("size", "warned"), [(TAG_TRUMP_SIZE, False), (TAG_TRUMP_SIZE + 1, True)])
def test_an_upload_warns_of_a_tag_over_the_limit(size: int, warned: bool) -> None:
    warning = f"{size} bytes of embedded tag exceeds the {TAG_TRUMP_SIZE}-byte limit (2.3.19, a trump reason): a.mp3"

    assert upload_warnings({"a.mp3": {**_track(), "tag size": size}}) == ([warning] if warned else [])


def test_an_upload_warns_of_a_non_standard_sample_rate() -> None:
    assert upload_warnings({"a.flac": _track(sample_rate=44056)}) == [
        "Non-standard sample rate 44056 Hz may be rejected: a.flac"
    ]


def test_an_upload_leaves_paths_id3_and_uncompressed_flacs_to_the_steps_before_it() -> None:
    raw = 44100 * 16 * 2
    track = {**_track(), "channels": 2, "bit rate": raw, "id3": True}

    assert upload_warnings({"x" * 300 + ".flac": track}) == []
    assert len(collect_upload_warnings("RED", "F", {"x" * 300 + ".flac": track})) == 3


@pytest.mark.parametrize(("codes", "limit"), [(["OPS"], 255), (["OPS", "RED"], 180), (["NOPE"], 180), ([], 180)])
def test_the_path_limit_is_the_strictest_of_the_trackers_rules(codes: list[str], limit: int) -> None:
    assert path_limit_for(codes) == limit


def test_an_unknown_tracker_sets_no_path_limit() -> None:
    assert path_limit_warning("NOPE", "x" * 1000) is None


def test_an_upload_run_prints_its_rule_warnings(flow, monkeypatch, capsys) -> None:  # noqa: F811
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(salmon.uploader, "upload_warnings", tag_rules.upload_warnings)
    monkeypatch.setattr(salmon.uploader, "concat_track_data", _sync({"01.flac": {"sample rate": 44056}}))

    _upload(None)

    assert "Non-standard sample rate 44056 Hz may be rejected: 01.flac" in capsys.readouterr().out
