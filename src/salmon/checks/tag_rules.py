"""Pre-upload path and sample-rate warnings for RED/OPS. Advisory only, never blocking."""

# Max full in-torrent path length (main folder + subfolders + filename): RED 2.3.12 = 180, OPS 2.3.12 = 255.
MAX_PATH_LENGTH = {"RED": 180, "OPS": 255}
STANDARD_SAMPLE_RATES = {44100, 48000, 88200, 96000, 176400, 192000}


def collect_upload_warnings(site_code: str, folder_name: str, track_data: dict) -> list[str]:
    """Return human-readable rule warnings for this upload; empty when clean."""
    warnings = []
    path_limit = MAX_PATH_LENGTH.get(site_code)
    for filename, track in track_data.items():
        full_path = f"{folder_name}/{filename}"
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
