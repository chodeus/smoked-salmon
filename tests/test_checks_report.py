"""salmon web's spectrals-page report: its sections, and the plots it names."""

from salmon.checks.report import spectrals_report

PROVENANCE = {"vendors": [], "markers": [], "contradictions": []}
INFO = {"sample rate": 96000, "precision": 24, "channels": 2, "bit rate": 4_600_000}
PLOTS_LINE = "Full-track spectrogram, a 2-second zoom, and an averaged frequency plot per track."


def test_the_spectrals_report_keeps_its_sections_and_names_the_plots() -> None:
    text = spectrals_report("/music/Album", {"01.flac": INFO}, PROVENANCE, [])

    lines = text.splitlines()
    assert lines[0] == "Album"
    assert "   Lossless (24-bit). Checking whether the audio matches that claim." in lines
    assert "   01.flac: 24-bit / 96 kHz stereo, 4600 kbps" in lines
    assert "   Plots below display 0 to 48 kHz, the full range for 96 kHz audio." in lines
    assert f"   {PLOTS_LINE}" in lines
    assert "5. About the release" in lines
    assert "Checks (salmon check all)" not in text
