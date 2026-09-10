"""Pre-upload path and sample-rate rules for RED/OPS.

Owns the path *measurement* for the whole codebase. The warnings here are advisory,
but folderstructure's blocking check measures the same way — two measurements that
disagree is how a folder passed one gate and failed the other.
"""

from salmon.constants import TAG_TRUMP_SIZE

# Max full in-torrent path: the top-level torrent folder, any subfolders, and the
# filename — nested folders and long classical filenames all count against it.
MAX_PATH_LENGTH = {"RED": 180, "OPS": 255}
# The folder is prepared once, before a tracker is chosen, so it has to satisfy the
# strictest destination it might go to.
STRICTEST_PATH_LENGTH = min(MAX_PATH_LENGTH.values())
STANDARD_SAMPLE_RATES = {44100, 48000, 88200, 96000, 176400, 192000}
# A FLAC whose audio bit rate is that of raw PCM was stored without compression (verbatim frames).
UNCOMPRESSED_RATIO = 0.99


def in_torrent_path(folder_name: str, relative_path: str) -> str:
    """The path a tracker counts: the torrent's top-level folder plus what sits under it.

    Not the on-disk path. Measuring from download_directory only coincides with this
    when the album happens to live there, and under-counts everywhere else.
    """
    return f"{folder_name}/{relative_path}" if relative_path not in ("", ".") else folder_name


def is_uncompressed(track: dict) -> bool:
    """True when the file's audio bit rate, less its tag block, is at least the raw PCM rate."""
    rate, bits, channels = track.get("sample rate"), track.get("precision"), track.get("channels")
    duration, bit_rate = track.get("duration"), track.get("bit rate")
    if not (rate and bits and channels and duration and bit_rate):
        return False
    audio_bit_rate = bit_rate - (track.get("tag size") or 0) * 8 / duration
    return audio_bit_rate >= UNCOMPRESSED_RATIO * rate * bits * channels


def collect_upload_warnings(site_code: str, folder_name: str, track_data: dict) -> list[str]:
    """Return human-readable rule warnings for this upload; empty when clean."""
    warnings = []
    path_limit = MAX_PATH_LENGTH.get(site_code)
    for filename, track in track_data.items():
        full_path = in_torrent_path(folder_name, filename)
        if path_limit and len(full_path) > path_limit:
            warnings.append(
                f"{len(full_path)}-char path exceeds {site_code}'s {path_limit} limit "
                f"(2.3.12, a trump reason): {full_path}"
            )
        tag_size = track.get("tag size")
        if tag_size is not None and tag_size > TAG_TRUMP_SIZE:
            warnings.append(
                f"{tag_size} bytes of embedded tag exceeds the {TAG_TRUMP_SIZE}-byte limit "
                f"(2.3.19, a trump reason): {filename}"
            )
        if filename.lower().endswith(".flac"):
            if track.get("id3"):
                warnings.append(
                    f"ID3 tag inside a FLAC (2.2.10.8, a trump reason); the integrity re-encode removes it: {filename}"
                )
            if is_uncompressed(track):
                warnings.append(f"Uncompressed FLAC (2.2.10.10, not allowed); recompress it (salmon up -c): {filename}")
        sample_rate = track.get("sample rate")
        precision = track.get("precision")
        if sample_rate and sample_rate not in STANDARD_SAMPLE_RATES:
            warnings.append(f"Non-standard sample rate {sample_rate} Hz may be rejected: {filename}")
        elif precision == 16 and sample_rate and sample_rate > 48000:
            # OPS forbids it outright; RED only makes it trumpable.
            if site_code == "OPS":
                warnings.append(
                    f"16-bit above 48 kHz is not permitted on OPS — downsample to 16/44.1 or 16/48: {filename}"
                )
            else:
                warnings.append(f"16-bit above 48 kHz is trumpable on RED — downsample to 16/44.1 or 16/48: {filename}")
    return warnings
