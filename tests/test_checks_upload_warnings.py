"""The fork's rule warnings: an upload's own list, and the full one salmon web's checks page shows."""

import pytest
from test_uploader_after_first_upload_fork import _sync, _upload, flow  # noqa: F401  # pyright: ignore[reportMissingImports]

import salmon.trackers
import salmon.uploader
from salmon.checks import tag_rules
from salmon.checks.high_rate import sixteen_bit_notice
from salmon.checks.tag_rules import path_limit_for, upload_warnings
from salmon.constants import TAG_TRUMP_SIZE


def _track(sample_rate=44100, precision=16):
    return {"sample rate": sample_rate, "precision": precision}


@pytest.mark.parametrize(("tracker", "ending"), [("RED", "RED can trump them."), ("OPS", "OPS refuses them.")])
def test_16bit_above_48k_is_each_trackers_own_rule(tracker, ending):
    files = {"a.flac": _track(sample_rate=96000, precision=16)}
    rule = salmon.trackers.tracker_classes[tracker].TAG_RULES.sixteen_bit_above_48khz
    notice = sixteen_bit_notice(tracker, rule, files)
    assert notice == f"1 16bit file(s) above 48 kHz: a.flac (96 kHz). {ending}"


@pytest.mark.parametrize(("size", "warned"), [(TAG_TRUMP_SIZE, False), (TAG_TRUMP_SIZE + 1, True)])
def test_an_upload_warns_of_a_tag_over_the_limit(size: int, warned: bool) -> None:
    warning = f"{size} bytes of embedded tag exceeds the {TAG_TRUMP_SIZE}-byte limit (2.3.19, a trump reason): a.mp3"

    assert upload_warnings({"a.mp3": {**_track(), "tag size": size}}) == ([warning] if warned else [])


def test_an_upload_warns_of_a_non_standard_sample_rate() -> None:
    assert upload_warnings({"a.flac": _track(sample_rate=44056)}) == [
        "Non-standard sample rate 44056 Hz may be rejected: a.flac"
    ]


@pytest.mark.parametrize(("codes", "limit"), [(["OPS"], 255), (["OPS", "RED"], 180), (["NOPE"], 180), ([], 180)])
def test_the_path_limit_is_the_strictest_of_the_trackers_rules(codes: list[str], limit: int) -> None:
    assert path_limit_for(codes) == limit


def test_an_upload_run_prints_its_rule_warnings(flow, monkeypatch, capsys) -> None:  # noqa: F811
    monkeypatch.setattr(salmon.uploader.cfg.upload, "multi_tracker_upload", False)
    monkeypatch.setattr(salmon.uploader, "upload_warnings", tag_rules.upload_warnings)
    monkeypatch.setattr(salmon.uploader, "concat_track_data", _sync({"01.flac": {"sample rate": 44056}}))

    _upload(None)

    assert "Non-standard sample rate 44056 Hz may be rejected: 01.flac" in capsys.readouterr().out
