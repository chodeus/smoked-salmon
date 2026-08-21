"""Who made these files, and does what they claim match what they are.

Rippers, stores and resellers stamp their own markers into the tags — 'EAC
FLAC -8', 'QOBUZ', 'hd24bit.com'. Those markers are the cheapest provenance
signal there is, and a marker that contradicts the audio (a 24bit claim on a
16bit file) is worth more than any spectrogram.
"""

import os
import re

from salmon.tagger.tags import gather_tags

# Vorbis comment keys mutagen lowercases; the ripper/store markers worth reading.
MARKER_FIELDS = (
    "comment",
    "encoded-by",
    "encodedby",
    "encoder",
    "encoder settings",
    "source",
    "sourceurl",
    "website",
    "url",
)

_URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|net|org|io|co|me|ru|to|cc|sh)\b", re.IGNORECASE)
_DEPTH_CLAIM_RE = re.compile(r"(\d{2})\s*-?\s*bit", re.IGNORECASE)


def _file_provenance(filename: str, tagfile) -> dict:
    """Vendor string, marker tags and the file's real bit depth."""
    mut = getattr(tagfile, "mut", None)
    tags = getattr(mut, "tags", None)
    markers: dict[str, str] = {}
    if tags is not None:
        for field in MARKER_FIELDS:
            try:
                values = tags[field]
            except (KeyError, TypeError):
                continue
            text = "; ".join(str(v) for v in values).strip() if isinstance(values, list) else str(values).strip()
            if text:
                markers[field] = text
    info = getattr(mut, "info", None)
    return {
        "file": filename,
        "vendor": getattr(tags, "vendor", None),
        "markers": markers,
        "bitdepth": getattr(info, "bits_per_sample", None),
    }


def _contradictions(files: list[dict]) -> list[str]:
    """Markers that claim a bit depth the audio does not have."""
    found = []
    for entry in files:
        depth = entry["bitdepth"]
        if not depth:
            continue
        for field, text in entry["markers"].items():
            # A depth inside a domain is part of the name of whoever ripped it
            # ("hd24bit.com"), not an assertion about this file. The URL still
            # shows up as a marker, so nothing is hidden — it just isn't a claim.
            for claim in _DEPTH_CLAIM_RE.findall(_URL_RE.sub(" ", text)):
                if int(claim) != depth:
                    found.append(f"{entry['file']}: {field} claims {claim}bit, the audio is {depth}bit")
    return found


def gather_provenance(path: str) -> dict:
    """Encoder and source markers across an album, plus any claim the audio contradicts."""
    try:
        tags = gather_tags(path)
    except Exception:
        return {"files": [], "vendors": [], "markers": [], "urls": [], "contradictions": []}

    files = [_file_provenance(name, tagfile) for name, tagfile in tags.items()]
    markers = {f"{field}: {text}" for entry in files for field, text in entry["markers"].items()}
    urls = {match for entry in files for text in entry["markers"].values() for match in _URL_RE.findall(text)}
    return {
        "files": files,
        "vendors": sorted({entry["vendor"] for entry in files if entry["vendor"]}),
        "markers": sorted(markers),
        "urls": sorted(urls),
        "contradictions": _contradictions(files),
    }


def describe(provenance: dict) -> str:
    """One line naming what was found, for a report or a verdict row."""
    parts = []
    if provenance["vendors"]:
        parts.append("encoded by " + ", ".join(provenance["vendors"]))
    if provenance["markers"]:
        parts.append("markers: " + "; ".join(provenance["markers"][:3]))
    return ". ".join(parts) if parts else "No encoder or source markers in the tags."


def album_provenance(path: str) -> dict:
    """gather_provenance for a directory, keyed for the checks payload."""
    return gather_provenance(os.fspath(path))
