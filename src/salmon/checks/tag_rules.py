"""Pre-upload path and sample-rate rules for RED/OPS.

Owns the path *measurement* for the whole codebase. The warnings here are advisory,
but folderstructure's blocking check measures the same way — two measurements that
disagree is how a folder passed one gate and failed the other.
"""

# Max full in-torrent path: the top-level torrent folder, any subfolders, and the
# filename — nested folders and long classical filenames all count against it.
MAX_PATH_LENGTH = {"RED": 180, "OPS": 255}
# The folder is prepared once, before a tracker is chosen, so it has to satisfy the
# strictest destination it might go to.
STRICTEST_PATH_LENGTH = min(MAX_PATH_LENGTH.values())
STANDARD_SAMPLE_RATES = {44100, 48000, 88200, 96000, 176400, 192000}


def in_torrent_path(folder_name: str, relative_path: str) -> str:
    """The path a tracker counts: the torrent's top-level folder plus what sits under it.

    Not the on-disk path. Measuring from download_directory only coincides with this
    when the album happens to live there, and under-counts everywhere else.
    """
    return f"{folder_name}/{relative_path}" if relative_path not in ("", ".") else folder_name


def collect_upload_warnings(site_code: str, folder_name: str, track_data: dict) -> list[str]:
    """Return human-readable rule warnings for this upload; empty when clean."""
    warnings = []
    path_limit = MAX_PATH_LENGTH.get(site_code)
    for filename, track in track_data.items():
        full_path = in_torrent_path(folder_name, filename)
        if path_limit and len(full_path) > path_limit:
            warnings.append(
                f"{len(full_path)}-char path exceeds {site_code}'s {path_limit} limit (a trump reason): {full_path}"
            )
        sample_rate = track.get("sample rate")
        precision = track.get("precision")
        if sample_rate and sample_rate not in STANDARD_SAMPLE_RATES:
            warnings.append(f"Non-standard sample rate {sample_rate} Hz may be rejected: {filename}")
        elif precision == 16 and sample_rate and sample_rate > 48000:
            # OPS 2.1.23.3.3 forbids it outright; RED 2.5.5.1 only makes it trumpable.
            if site_code == "OPS":
                warnings.append(
                    f"16-bit above 48 kHz is not permitted on OPS (2.1.23.3.3) — "
                    f"downsample to 16/44.1 or 16/48: {filename}"
                )
            else:
                warnings.append(
                    f"16-bit above 48 kHz is trumpable (RED 2.5.5.1) — downsample to 16/44.1 or 16/48: {filename}"
                )
    return warnings
