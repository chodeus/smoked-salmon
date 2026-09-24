import os
from dataclasses import dataclass, field
from importlib import import_module

import anyio
import cambia
import pytest

from salmon.errors import CRCMismatchError, EditedLogError, LogCheckSkipped

# salmon.checks.__init__ likely defines click commands that could shadow submodules on the
# package object, so importlib is used to get the module itself, matching test_checks_integrity.
logs = import_module("salmon.checks.logs")


@dataclass
class FakeTestAndCopy:
    copy_hash: str


@dataclass
class FakeTrack:
    num: int
    copy_hash: str
    is_range: bool = False

    @property
    def test_and_copy(self) -> FakeTestAndCopy:
        return FakeTestAndCopy(self.copy_hash)


@dataclass
class FakeTocHash:
    hash: str


@dataclass
class FakeTocRaw:
    entries: list = field(default_factory=list)


@dataclass
class FakeToc:
    accurip_tocid: FakeTocHash
    raw: FakeTocRaw = field(default_factory=FakeTocRaw)


@dataclass
class FakeChecksum:
    integrity: cambia.Integrity = field(default_factory=lambda: cambia.Integrity.Match)


@dataclass
class FakeParsedLog:
    tracks: list[FakeTrack]
    toc_hash: str = "disc-1"
    checksum: FakeChecksum = field(default_factory=FakeChecksum)

    @property
    def toc(self) -> FakeToc:
        return FakeToc(accurip_tocid=FakeTocHash(hash=self.toc_hash))


@dataclass
class FakeParsedCombined:
    parsed_logs: list[FakeParsedLog]


@dataclass
class FakeEvaluationCombined:
    combined_score: str = "100"


@dataclass
class FakeCambiaOutput:
    parsed: FakeParsedCombined
    evaluation_combined: list[FakeEvaluationCombined] = field(default_factory=lambda: [FakeEvaluationCombined()])


def _patch_cambia(monkeypatch, output: FakeCambiaOutput) -> None:
    monkeypatch.setattr(logs.cambia, "parse_log_file", lambda path: output)


def _patch_file_crcs(monkeypatch, crc_by_name: dict[str, str]) -> None:
    async def fake_calculate_file_crc_async(filepath: str, _: object = None) -> str:
        return crc_by_name[os.path.basename(filepath)]

    monkeypatch.setattr(logs, "_calculate_file_crc_async", fake_calculate_file_crc_async)


def _write_files(tmp_path, names: list[str]) -> str:
    for name in names:
        (tmp_path / name).write_bytes(b"audio")
    return str(tmp_path)


def test_appended_rerip_replaces_the_stale_hash(tmp_path, monkeypatch) -> None:
    basepath = _write_files(tmp_path, ["01.flac", "02.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(tracks=[FakeTrack(num=1, copy_hash="STALE1"), FakeTrack(num=2, copy_hash="MATCH2")]),
                # Appended rerip log for track 1 only, with the current, correct hash.
                FakeParsedLog(tracks=[FakeTrack(num=1, copy_hash="MATCH1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"01.flac": "MATCH1", "02.flac": "MATCH2"})

    anyio.run(logs.check_log_cambia, "log.log", basepath)


def test_two_discs_with_overlapping_track_numbers_keep_both_hashes(tmp_path, monkeypatch) -> None:
    # Keyed by track number alone, disc 2's hash would drop disc 1's, hiding its corrupt track.
    basepath = _write_files(tmp_path, ["d1-01.flac", "d2-01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "CORRUPTED", "d2-01.flac": "D2-1"})

    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


def test_a_real_mismatch_still_raises(tmp_path, monkeypatch) -> None:
    basepath = _write_files(tmp_path, ["01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=[FakeTrack(num=1, copy_hash="EXPECTED")])])
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"01.flac": "DIFFERENT"})

    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


def test_multi_disc_range_rip_is_skipped_with_a_notice(tmp_path, monkeypatch, capsys) -> None:
    basepath = _write_files(tmp_path, ["range1.flac", "range2.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="R1", is_range=True)]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="R2", is_range=True)]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)

    async def fail_range_crc(*args: object, **kwargs: object) -> str:
        raise AssertionError("range CRC should not be computed for a skipped multi-disc range rip")

    monkeypatch.setattr(logs, "_calculate_range_crc_async", fail_range_crc)

    anyio.run(logs.check_log_cambia, "log.log", basepath)

    out = capsys.readouterr().out
    assert "Multi-disc range rip" in out


def _write_discs(tmp_path, discs: dict[str, list[str]]) -> str:
    for disc, names in discs.items():
        (tmp_path / disc).mkdir()
        for name in names:
            (tmp_path / disc / name).write_bytes(b"audio")
    return str(tmp_path)


def test_an_unparseable_log_is_skipped(tmp_path, monkeypatch) -> None:
    def parse_log_file(_path):
        raise ValueError("not a log")

    monkeypatch.setattr(logs.cambia, "parse_log_file", parse_log_file)
    with pytest.raises(LogCheckSkipped, match="not a log"):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), str(tmp_path))


def test_an_unreadable_log_file_is_an_error_not_a_skip(tmp_path, monkeypatch) -> None:
    def parse_log_file(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(logs.cambia, "parse_log_file", parse_log_file)
    with pytest.raises(PermissionError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), str(tmp_path))


def test_a_log_without_a_score_is_still_checked(tmp_path, monkeypatch, capsys) -> None:
    disc = _write_files(tmp_path, ["d1-01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=[FakeTrack(1, "D1-1")])]),
        evaluation_combined=[],
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "OTHER"})
    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), disc)
    out = capsys.readouterr().out
    assert "Could not read the log score" in out


def test_a_log_with_no_audio_is_skipped(tmp_path, monkeypatch) -> None:
    output = FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=[FakeTrack(1, "D1-1")])]))
    _patch_cambia(monkeypatch, output)
    with pytest.raises(LogCheckSkipped, match="No audio files found"):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), str(tmp_path))


def _fail_scandir(monkeypatch, folder: str, error: OSError) -> None:
    real_scandir = os.scandir

    def scandir(path):
        if os.path.basename(path) == folder:
            raise error
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)


def test_a_multi_disc_track_rip_log_in_a_disc_folder_checks_every_disc(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac"], "CD2": ["d2-01.flac"]})
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    logpath = str(tmp_path / "CD1" / "rip.log")

    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})
    anyio.run(logs.check_log_cambia, logpath, basepath)

    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "CORRUPTED"})
    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, logpath, basepath)


def test_a_one_disc_range_rip_is_rebuilt_from_its_own_disc_folder(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac", "d1-02.flac"], "CD2": ["d2-01.flac"]})
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="R1", is_range=True)])]
        )
    )
    _patch_cambia(monkeypatch, output)
    rebuilt_from: list[list[str]] = []

    async def fake_range_crc(track_files: list[str], _toc_entries: list) -> str:
        rebuilt_from.append(sorted(os.path.basename(f) for f in track_files))
        return "R1"

    monkeypatch.setattr(logs, "_calculate_range_crc_async", fake_range_crc)

    anyio.run(logs.check_log_cambia, str(tmp_path / "CD1" / "rip.log"), basepath)

    assert rebuilt_from == [["d1-01.flac", "d1-02.flac"]]


def test_a_multi_disc_log_missing_other_discs_audio_is_skipped_not_failed(tmp_path, monkeypatch, capsys) -> None:
    # Only one disc's audio under the search root: a skip, not a CRC mismatch.
    disc = _write_files(tmp_path, ["d1-01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1"})

    anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), disc)

    out = capsys.readouterr().out
    assert "only 1 audio file" in out


def test_an_unreadable_disc_folder_is_an_error_not_a_skip(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac"], "CD2": ["d2-01.flac"]})
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})
    _fail_scandir(monkeypatch, "CD2", PermissionError(13, "Permission denied"))
    with pytest.raises(PermissionError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "CD1" / "rip.log"), basepath)


def test_an_unreadable_search_root_is_an_error_not_no_audio(tmp_path, monkeypatch) -> None:
    disc = _write_files(tmp_path, ["d1-01.flac"])
    output = FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=[FakeParsedLog(tracks=[FakeTrack(1, "D1-1")])]))
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1"})
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if os.fspath(path) == disc:
            raise PermissionError(13, "Permission denied", path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    with pytest.raises(PermissionError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "rip.log"), disc)


def test_a_disc_folder_that_vanishes_mid_scan_is_an_error(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac"], "CD2": ["d2-01.flac"]})
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})
    _fail_scandir(monkeypatch, "CD2", FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(FileNotFoundError):
        anyio.run(logs.check_log_cambia, str(tmp_path / "CD1" / "rip.log"), basepath)


def test_checklog_on_a_folder_checks_each_log_against_that_folder(tmp_path, monkeypatch) -> None:
    basepath = _write_discs(tmp_path, {"CD1": ["d1-01.flac"], "CD2": ["d2-01.flac"]})
    (tmp_path / "CD1" / "rip.log").write_text("log")
    seen: list[tuple[str, str]] = []

    async def fake_check(logpath: str, base: str) -> None:
        seen.append((logpath, base))

    checks = import_module("salmon.checks")
    monkeypatch.setattr(checks, "check_log_cambia", fake_check)

    anyio.run(checks.log.callback, basepath)

    assert seen == [(str(tmp_path / "CD1" / "rip.log"), basepath)]


def test_a_range_rip_on_a_later_disc_skips_the_combined_check(tmp_path, monkeypatch, capsys) -> None:
    basepath = _write_files(tmp_path, ["d1-01.flac", "d2-range.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="R2", is_range=True)]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    # Disc 2's range CRC matches no single file, so a per-file check would call a good rip a mismatch.
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-range.flac": "FILE-CRC"})

    anyio.run(logs.check_log_cambia, "log.log", basepath)

    out = capsys.readouterr().out
    assert "Multi-disc range rip" in out


def test_a_range_entry_replaced_by_a_rerip_is_verified(tmp_path, monkeypatch, capsys) -> None:
    basepath = _write_files(tmp_path, ["d1-01.flac", "d2-01.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="R2", is_range=True)]),
                # Appended rerip of disc 2 as a track rip: the latest entry is what's checked.
                FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})

    anyio.run(logs.check_log_cambia, "log.log", basepath)

    out = capsys.readouterr().out
    assert "All CRC values match" in out


@pytest.mark.parametrize("multi_disc", [True, False], ids=["multi-disc", "one-disc"])
def test_a_first_log_range_rip_replaced_by_a_rerip_is_verified_per_file(
    tmp_path, monkeypatch, capsys, multi_disc
) -> None:
    names = ["d1-01.flac", "d2-01.flac"] if multi_disc else ["d1-01.flac"]
    basepath = _write_files(tmp_path, names)
    logs_ = [
        FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="R1", is_range=True)]),
        FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="D1-1")]),
    ]
    if multi_disc:
        logs_.append(FakeParsedLog(toc_hash="disc-2", tracks=[FakeTrack(num=1, copy_hash="D2-1")]))
    _patch_cambia(monkeypatch, FakeCambiaOutput(parsed=FakeParsedCombined(parsed_logs=logs_)))
    _patch_file_crcs(monkeypatch, {"d1-01.flac": "D1-1", "d2-01.flac": "D2-1"})

    async def no_range(*_args, **_kwargs) -> str:
        raise AssertionError("the range was replaced; nothing should be rebuilt from its TOC")

    monkeypatch.setattr(logs, "_calculate_range_crc_async", no_range)

    anyio.run(logs.check_log_cambia, "log.log", basepath)

    out = capsys.readouterr().out
    assert "All CRC values match" in out


@pytest.mark.parametrize("multi_disc", [True, False], ids=["multi-disc", "one-disc"])
def test_two_tracks_sharing_a_crc_each_need_a_matching_file(tmp_path, monkeypatch, multi_disc) -> None:
    # The same track on two discs, or two silent tracks: one good copy must not cover a corrupt one.
    basepath = _write_files(tmp_path, ["a.flac", "b.flac"])
    second_disc = "disc-2" if multi_disc else "disc-1"
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="SAME")]),
                FakeParsedLog(toc_hash=second_disc, tracks=[FakeTrack(num=2, copy_hash="SAME")]),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"a.flac": "SAME", "b.flac": "CORRUPTED"})

    with pytest.raises(CRCMismatchError):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


@pytest.mark.parametrize("multi_disc", [True, False], ids=["multi-disc", "one-disc"])
def test_an_edited_appended_log_is_refused(tmp_path, monkeypatch, multi_disc) -> None:
    basepath = _write_files(tmp_path, ["a.flac", "b.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(toc_hash="disc-1", tracks=[FakeTrack(num=1, copy_hash="A")]),
                FakeParsedLog(
                    toc_hash="disc-2" if multi_disc else "disc-1",
                    tracks=[FakeTrack(num=2, copy_hash="B")],
                    checksum=FakeChecksum(integrity=cambia.Integrity.Mismatch),
                ),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"a.flac": "A", "b.flac": "B"})

    with pytest.raises(EditedLogError):
        anyio.run(logs.check_log_cambia, "log.log", basepath)


def test_an_appended_log_without_a_checksum_warns(tmp_path, monkeypatch, capsys) -> None:
    basepath = _write_files(tmp_path, ["a.flac"])
    output = FakeCambiaOutput(
        parsed=FakeParsedCombined(
            parsed_logs=[
                FakeParsedLog(tracks=[FakeTrack(num=1, copy_hash="STALE")]),
                FakeParsedLog(
                    tracks=[FakeTrack(num=1, copy_hash="A")],
                    checksum=FakeChecksum(integrity=cambia.Integrity.Unknown),
                ),
            ]
        )
    )
    _patch_cambia(monkeypatch, output)
    _patch_file_crcs(monkeypatch, {"a.flac": "A"})

    anyio.run(logs.check_log_cambia, "log.log", basepath)

    out = capsys.readouterr().out
    assert "Lacking a valid checksum" in out
