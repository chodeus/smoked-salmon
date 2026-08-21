"""A plain-text summary of what a release actually is.

Laid out the way tracker help threads ask for it — what it claims to be, how it
was encoded, its real numbers, the plots, and anything about the music that
changes how a spectrogram should be read — so it can be pasted straight in.
"""

import os

from salmon.uploader.frequency import SpectrumResult


def _format_line(info: dict) -> str:
    precision = info.get("precision")
    rate = info.get("sample rate")
    depth = f"{precision}-bit" if precision else "unknown depth"
    khz = f"{rate / 1000:g} kHz" if rate else "unknown rate"
    channel_count = info.get("channels")
    channels = "mono" if channel_count == 1 else "stereo" if channel_count == 2 else f"{channel_count}ch"
    bitrate = info.get("bit rate")
    kbps = f"{bitrate / 1000:.0f} kbps" if bitrate else "unknown bitrate"
    return f"{depth} / {khz} {channels}, {kbps}"


def build_report(
    album_path: str,
    audio_info: dict,
    provenance: dict,
    spectra: list[SpectrumResult] | None = None,
    image_urls: list[str] | None = None,
) -> str:
    """Assemble the report from data the checks already gathered."""
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
    for result in spectra:
        if result.error:
            lines.append(f"   {result.file}: could not be analysed ({result.error})")
        else:
            lines.append(f"   {result.file}: energy to {result.cutoff_hz / 1000:.1f} kHz")
    if image_urls:
        lines += [f"   {url}" for url in image_urls]
    else:
        lines.append("   Full-track spectrogram, a 2-second zoom, and an averaged frequency plot per track.")

    lines += ["", "5. About the release", "   "]
    return "\n".join(lines)
