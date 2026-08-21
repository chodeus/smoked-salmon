"""Upconvert check: out-of-scope files are skipped, real failures are reported."""

import salmon.checks.upconverts as uc
from salmon.errors import UpconvertCheckError, UpconvertCheckNotApplicable


async def test_16bit_is_skipped_without_a_warning(monkeypatch, capsys):
    async def out_of_scope(_filepath):
        raise UpconvertCheckNotApplicable("This is a 16bit FLAC file.")

    monkeypatch.setattr(uc, "check_upconvert", out_of_scope)

    assert await uc._upconvert_check_handler("/music/album/01.flac") is None
    # A 16bit album would otherwise warn once per track about nothing being wrong.
    assert capsys.readouterr().out == ""


async def test_a_real_failure_still_names_the_file_and_the_reason(monkeypatch, capsys):
    async def broken(_filepath):
        raise UpconvertCheckError("File appears to be corrupt: bad frame")

    monkeypatch.setattr(uc, "check_upconvert", broken)

    assert await uc._upconvert_check_handler("/music/album/02.flac") is None
    printed = capsys.readouterr().out
    assert "02.flac" in printed
    assert "bad frame" in printed
