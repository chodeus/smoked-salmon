"""Remember what each converted folder was made from, so a later upload can describe the conversion."""

import contextlib
import json
import os
import tempfile
from typing import Any

import asyncclick as click

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
    """The recorded facts for a folder a converter produced; None when there are none or they are unusable.

    A sidecar that cannot be read at all raises: an upload of a converted folder owes the site a
    description, so a permission or I/O failure must not read as "this folder was never converted".
    """
    try:
        with open(_sidecar(folder), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        click.secho(f"Ignoring an unreadable conversion record for {os.path.basename(folder)}.", fg="yellow")
        return None
    return data if _usable(data) else None


def carry_conversion(old: str, new: str) -> None:
    """Hand a renamed or copied folder's record to its new name; the old record goes once the old folder has."""
    facts = conversion_of(old)
    if facts is None or os.path.abspath(old) == os.path.abspath(new):
        return
    record_conversion(new, **facts)
    if not os.path.isdir(old):
        with contextlib.suppress(OSError):
            os.remove(_sidecar(old))


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
