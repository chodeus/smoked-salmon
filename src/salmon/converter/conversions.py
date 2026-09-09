"""Remember what each converted folder was made from, so a later upload can describe the transcode."""

import json
import os
from typing import Any

# One hidden file per parent directory, keyed by the converted folder's name; never inside the album.
REGISTRY = ".salmon-conversions.json"


def _registry_path(folder: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(folder)), REGISTRY)


def _load(registry: str) -> dict[str, Any]:
    try:
        with open(registry, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_conversion(output: str, **facts: Any) -> None:
    """Note how `output` was produced: its source folder plus the converter's settings."""
    registry = _registry_path(output)
    data = _load(registry)
    data[os.path.basename(os.path.abspath(output))] = facts
    with open(registry + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(registry + ".tmp", registry)


def conversion_of(folder: str) -> dict[str, Any] | None:
    """The recorded facts for a folder a converter produced, else None."""
    return _load(_registry_path(folder)).get(os.path.basename(os.path.abspath(folder)))
