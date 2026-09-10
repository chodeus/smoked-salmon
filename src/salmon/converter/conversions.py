"""Remember what each converted folder was made from, so a later upload can describe the conversion."""

import json
import os
import tempfile
from typing import Any

# One hidden directory per parent with one file per converted folder: never inside the album, never shared by writers.
REGISTRY_DIR = ".salmon-conversions"
KINDS = {"downconvert", "transcode"}


def _sidecar(folder: str) -> str:
    folder = os.path.abspath(folder)
    return os.path.join(os.path.dirname(folder), REGISTRY_DIR, os.path.basename(folder) + ".json")


def record_conversion(output: str, **facts: Any) -> None:
    """Note how `output` was produced: its source folder plus the converter's settings."""
    sidecar = _sidecar(output)
    os.makedirs(os.path.dirname(sidecar), exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(sidecar), prefix=os.path.basename(sidecar), suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        json.dump(facts, fh, indent=2, sort_keys=True)
    os.replace(temp, sidecar)


def conversion_of(folder: str) -> dict[str, Any] | None:
    """The recorded facts for a folder a converter produced; None when there are none or they are unusable."""
    try:
        with open(_sidecar(folder), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if _usable(data) else None


def _usable(data: Any) -> bool:
    from salmon.converter.downconverting import SOX_DEPTH_ARGS  # local: both converters import this module
    from salmon.converter.transcoding import LAME_COMMAND_MAP

    if not isinstance(data, dict) or data.get("kind") not in KINDS or not isinstance(data.get("source"), str):
        return False
    if data["kind"] == "transcode":
        return data.get("bitrate") in LAME_COMMAND_MAP
    return data.get("bit_depth") in SOX_DEPTH_ARGS and _usable_rates(data.get("sample_rate"))


def _usable_rates(rates: Any) -> bool:
    if rates is None or _positive_int(rates):
        return True
    return isinstance(rates, list) and bool(rates) and all(map(_positive_int, rates))


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
