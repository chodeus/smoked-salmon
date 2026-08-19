"""Pre-upload path and sample-rate warnings for RED/OPS. Advisory only, never blocking."""

RED_MAX_PATH_LENGTH = 180
STANDARD_SAMPLE_RATES = {44100, 48000, 88200, 96000, 176400, 192000}


def collect_upload_warnings(site_code: str, folder_name: str, track_data: dict) -> list[str]:
    """Return human-readable rule warnings for this upload; empty when clean."""
    warnings = []
    for filename, track in track_data.items():
        full_path = f"{folder_name}/{filename}"
        if site_code == "RED" and len(full_path) > RED_MAX_PATH_LENGTH:
            warnings.append(
                f"{len(full_path)}-char path exceeds RED's {RED_MAX_PATH_LENGTH} limit (a trump reason): {full_path}"
            )
        sample_rate = track.get("sample rate")
        precision = track.get("precision")
        if sample_rate and sample_rate not in STANDARD_SAMPLE_RATES:
            warnings.append(f"Non-standard sample rate {sample_rate} Hz may be rejected: {filename}")
        elif precision == 16 and sample_rate and sample_rate > 48000:
            warnings.append(
                f"16-bit above 48 kHz is trumpable (RED 2.5.5.1) — downsample to 16/44.1 or 16/48: {filename}"
            )
    return warnings
