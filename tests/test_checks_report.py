"""check all's report: the release's numbers, its FLACs' frequency analysis and the verdict rows, naming no parent."""

import os
from pathlib import Path

import anyio
from asyncclick.testing import CliRunner

from salmon.checks import all_checks
from salmon.checks import report as report_module
from salmon.checks.report import album_report, build_report
from salmon.errors import UploadError

PROVENANCE = {"vendors": [], "markers": [], "contradictions": []}
INFO = {"sample rate": 44100, "precision": 16, "channels": 2, "bit rate": 900000}
PLOTS_LINE = "Full-track spectrogram, a 2-second zoom, and an averaged frequency plot per track."
ROWS = [
    {"id": "log", "label": "Rip log", "verdict": "warn", "detail": "rip.log: score 90/100.", "notes": ["rip.log: x"]},
    {"id": "mqa", "label": "MQA", "verdict": "ok", "detail": "No MQA encoding detected.", "notes": []},
]


def test_without_rows_the_report_keeps_its_sections() -> None:
    text = build_report("/music/Album", {}, PROVENANCE)

    assert "5. About the release" in text
    assert "Checks (salmon check all)" not in text
    assert PLOTS_LINE in text


def test_rows_add_a_checks_section_with_their_notes() -> None:
    text = build_report("/music/Album", {}, PROVENANCE, rows=ROWS)

    lines = text.splitlines()
    start = lines.index("5. Checks (salmon check all)")
    assert lines[start + 1 : start + 4] == [
        "   WARN  Rip log: rip.log: score 90/100.",
        "         rip.log: x",
        "   OK    MQA: No MQA encoding detected.",
    ]
    assert "6. About the release" in lines
    # check all removes its plots, so it names none.
    assert PLOTS_LINE not in text


def test_the_report_analyses_the_flacs_outside_the_album_and_names_no_parent(tmp_path, monkeypatch) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    for name in ("01.flac", "02.mp3"):
        (album / name).write_bytes(b"audio")
    before = sorted(os.listdir(album))
    analysed: list[tuple[str, list[str], str]] = []

    async def frequency(path: str, files: list[str], out_dir: str) -> list:
        analysed.append((path, files, out_dir))
        Path(out_dir, "01 Frequency.png").write_bytes(b"png")
        return []

    monkeypatch.setattr(report_module, "audio_file_info", lambda _path, _name: INFO)
    monkeypatch.setattr(report_module, "generate_frequency_plots", frequency)
    rows = [{"id": "integrity", "label": "File integrity", "verdict": "block", "detail": f"{album}/01.flac: bad"}]

    text = anyio.run(album_report, str(album), rows, PROVENANCE)

    [(path, files, out_dir)] = analysed
    assert (path, files) == (str(album), ["01.flac"])
    assert not out_dir.startswith(str(album))
    assert not os.path.exists(out_dir), "the plots' folder is removed"
    assert sorted(os.listdir(album)) == before
    assert str(tmp_path) not in text
    assert "BLOCK File integrity: Album/01.flac: bad" in text


def test_a_file_that_cannot_be_read_leaves_the_others_in_the_report(tmp_path, monkeypatch) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    for name in ("01.flac", "02.flac"):
        (album / name).write_bytes(b"audio")
    analysed: list[list[str]] = []

    async def frequency(_path: str, files: list[str], _out_dir: str) -> list:
        analysed.append(files)
        return []

    def read(_path: str, name: str) -> dict:
        if name == "02.flac":
            raise UploadError(f"Could not read audio file: {name}")
        return INFO

    monkeypatch.setattr(report_module, "audio_file_info", read)
    monkeypatch.setattr(report_module, "generate_frequency_plots", frequency)

    text = anyio.run(album_report, str(album), [], PROVENANCE)

    assert analysed == [["01.flac"]]
    assert "   01.flac: 16-bit / 44.1 kHz stereo, 900 kbps" in text


async def test_a_blocked_release_still_gets_its_report(album_dir, monkeypatch) -> None:
    from salmon.checks import preflight as pf

    async def no_analysis(_path: str, _files: list[str], _out_dir: str) -> list:
        return []

    monkeypatch.setattr(
        pf, "detect_source", lambda _p: {"source": "WEB", "confidence": "confirmed", "reasons": ["store tag"]}
    )
    monkeypatch.setattr(report_module, "generate_frequency_plots", no_analysis)

    result = await CliRunner().invoke(all_checks, [str(album_dir), "--source", "WEB", "--report"])

    assert result.exit_code == 1, result.output
    assert "5. Checks (salmon check all)" in result.output
    assert "Not suitable for upload" in result.output
