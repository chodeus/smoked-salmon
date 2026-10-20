"""A request the upload pipeline goes on past without knowing whether the tracker acted on it is recorded."""

from typing import Any, cast

from salmon.errors import UnknownOutcomeError
from salmon.uploader import spectrals
from salmon.uploader.record import recording


class Site:
    site_code = site_string = "RED"
    base_url = "http://127.0.0.1:9"

    def __init__(self, lost: UnknownOutcomeError) -> None:
        self.lost = lost

    async def report_lossy_master(self, *_args: Any) -> None:
        raise self.lost

    async def append_to_torrent_description(self, *_args: Any) -> None:
        raise self.lost


async def test_a_lossy_report_whose_answer_is_lost_is_recorded():
    lost = UnknownOutcomeError("RED answered 502")
    with recording() as record:
        await spectrals.report_lossy_master(cast("Any", Site(lost)), 77, None, None, "WEB", "Lossy master.")
    assert record.unknown_outcomes == [lost]


async def test_a_description_edit_whose_answer_is_lost_is_recorded(monkeypatch, tmp_path):
    lost = UnknownOutcomeError("RED answered 502 on a later hop")

    async def checked(*_args: Any, **_kwargs: Any) -> tuple[bool, dict[int, str]]:
        return False, {1: "01. one.flac"}

    async def uploaded(*_args: Any) -> dict[int, list[str]]:
        return {1: ["https://images.test/1.png", "https://images.test/2.png"]}

    monkeypatch.setattr(spectrals, "check_spectrals", checked)
    monkeypatch.setattr(spectrals, "handle_spectrals_upload_and_deletion", uploaded)
    with recording() as record:
        await spectrals.post_upload_spectral_check(
            cast("Any", Site(lost)), str(tmp_path), 77, None, {"01. one.flac": {}}, "WEB", None
        )
    assert record.unknown_outcomes == [lost]


async def test_nothing_is_kept_outside_a_recording():
    await spectrals.report_lossy_master(cast("Any", Site(UnknownOutcomeError("lost"))), 77, None, None, "WEB", "x")
    with recording() as record:
        pass
    assert record.unknown_outcomes == []
