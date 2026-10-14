"""A plain-text summary of what a release actually is.

Laid out the way tracker help threads ask for it — what it claims to be, how it
was encoded, its real numbers, the plots, and anything about the music that
changes how a spectrogram should be read — so it can be pasted straight in.
"""

import asyncio
import os
import tempfile

from salmon.checks.album import in_album
from salmon.checks.provenance import gather_provenance
from salmon.common import get_audio_files
from salmon.tagger.audio_info import audio_file_info
from salmon.uploader.frequency import SpectrumResult, describe, generate_frequency_plots, summarize


def _format_line(info: dict) -> str:
    precision = info.get("precision")
    rate = info.get("sample rate")
    depth = f"{precision}-bit" if precision else "unknown depth"
    khz = f"{rate / 1000:g} kHz" if rate else "unknown rate"
    channel_count = info.get("channels")
    if channel_count == 1:
        channels = "mono"
    elif channel_count == 2:
        channels = "stereo"
    else:
        channels = f"{channel_count}ch" if channel_count else "unknown channels"
    bitrate = info.get("bit rate")
    kbps = f"{bitrate / 1000:.0f} kbps" if bitrate else "unknown bitrate"
    return f"{depth} / {khz} {channels}, {kbps}"


def build_report(
    album_path: str,
    audio_info: dict,
    provenance: dict,
    spectra: list[SpectrumResult] | None = None,
    image_urls: list[str] | None = None,
    rows: list[dict] | None = None,
) -> str:
    """Assemble the report from data the checks already gathered; rows adds check all's verdicts."""
    spectra = spectra or []
    lines: list[str] = [f"{os.path.basename(album_path)}", ""]

    lossless = {i.get("precision") for i in audio_info.values() if i.get("precision")}
    lines.append("1. Lossless or lossy")
    if lossless:
        depths = ", ".join(f"{d}-bit" for d in sorted(lossless))
        lines.append(f"   Lossless ({depths}). Checking whether the audio matches that claim.")
    else:
        lines.append("   Lossy, or the bit depth could not be read.")
    if provenance.get("contradictions"):
        for note in provenance["contradictions"]:
            lines.append(f"   Claim does not match the audio — {note}.")

    lines += ["", "2. Format and encoder"]
    vendors = provenance.get("vendors") or []
    lines.append(f"   Encoder: {', '.join(vendors)}" if vendors else "   Encoder: not recorded in the tags.")
    for marker in provenance.get("markers", [])[:5]:
        lines.append(f"   Tag marker: {marker}")

    lines += ["", "3. Bitrate, sample rate and bit depth"]
    for name, info in audio_info.items():
        lines.append(f"   {name}: {_format_line(info)}")
    rates = {i.get("sample rate") for i in audio_info.values() if i.get("sample rate")}
    if rates:
        top = max(rates)
        lines.append(f"   Plots below display 0 to {top / 2000:g} kHz, the full range for {top / 1000:g} kHz audio.")

    lines += ["", "4. Plots"]
    lines += [f"   {describe(result)}" for result in spectra]
    lines += [f"   {note}" for note in summarize(spectra)[1]]
    if image_urls:
        lines += [f"   {url}" for url in image_urls]
    elif rows is None:
        # check all keeps no plots: only the spectrals page has images to name.
        lines.append("   Full-track spectrogram, a 2-second zoom, and an averaged frequency plot per track.")

    if rows is not None:
        lines += ["", "5. Checks (salmon check all)"]
        for row in rows:
            lines.append(f"   {row['verdict'].upper():<5} {row['label']}: {row['detail']}")
            lines += [f"         {note}" for note in row.get("notes") or ()]
    lines += ["", f"{6 if rows is not None else 5}. About the release", "   "]
    return "\n".join(lines)


def _readable_audio_info(path: str) -> dict[str, dict]:
    """Each audio file's numbers; a file that cannot be read is left out, not the whole album."""
    info = {}
    for name in get_audio_files(path, True):
        try:
            info[name] = audio_file_info(path, name)
        except Exception:
            continue
    return info


async def album_report(path: str, rows: list[dict], provenance: dict | None = None) -> str:
    """check all's report: the album's numbers, each FLAC's frequency analysis, and the rows; no parent path."""
    audio_info = await asyncio.to_thread(_readable_audio_info, path)
    if provenance is None:
        provenance = await asyncio.to_thread(gather_provenance, path)
    flacs = [name for name in audio_info if name.lower().endswith(".flac")]
    # The plots go to a folder of their own, removed after: nothing is written into the album.
    with tempfile.TemporaryDirectory() as plots:
        spectra = await generate_frequency_plots(path, flacs, plots) if flacs else []
    return in_album(build_report(path, audio_info, provenance, spectra, rows=rows), path)
