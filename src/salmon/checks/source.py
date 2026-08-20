"""Infer an album's media source from its files, tags and audio properties.

Only reports a source when the evidence is unambiguous. A plain 16/44 rip with no
log is genuinely undecidable, so it returns unknown rather than guessing — naming
the wrong source on an upload breaks tracker rules.
"""

import os
import re

from mutagen import File as MutagenFile

from salmon.common.files import get_audio_files

# Read enough of a log to catch the ripper banner without slurping a huge file.
_LOG_HEAD_BYTES = 4096
_RIPPER_SIGNATURES = re.compile(
    r"exact audio copy|\bxld\b|x lossless decoder|whipper|morituri|dbpoweramp|cueripper|accuraterip",
    re.IGNORECASE,
)
# Tags only a digital storefront writes.
_STORE_TAGS = (
    (re.compile(r"(^|\n|:)asin="), "Amazon ASIN tag"),
    (re.compile(r"com\.apple\.itunes|(^|\n)apid=|(^|\n)purchase[ _]?date="), "iTunes purchase tags"),
    (re.compile(r"bandcamp\.com"), "Bandcamp tag"),
    (re.compile(r"(^|\n)(www|website|url)=[^\n]*(qobuz|bandcamp|beatport|7digital|hdtracks)"), "store URL tag"),
)
_MEDIA_TAG = re.compile(r"(?:^|\n)(?:media|sourcemedia|tmed)=\[?'?([a-z0-9 ]+)")
_MEDIA_VALUES = {
    "cd": "CD",
    "compact disc": "CD",
    "web": "WEB",
    "digital media": "WEB",
    "file": "WEB",
    "digital": "WEB",
    "vinyl": "Vinyl",
    "lp": "Vinyl",
    "12": "Vinyl",
    "cassette": "Cassette",
    "sacd": "SACD",
    "dvd": "DVD",
}
_VINYL_TRACKNO = re.compile(r"^([A-H])[0-9]{1,2}$", re.IGNORECASE)


def _tag_blob(mut) -> str:
    """Flatten one file's tags to lowercase `key=value` lines, format-agnostic."""
    if not mut.tags:
        return ""
    return "\n".join(f"{key}={value}".lower() for key, value in dict(mut.tags).items())


def _has_rip_log(path: str) -> str | None:
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            if not name.lower().endswith(".log"):
                continue
            try:
                with open(os.path.join(root, name), "rb") as fh:
                    head = fh.read(_LOG_HEAD_BYTES).decode("utf-8", "ignore")
            except OSError:
                continue
            match = _RIPPER_SIGNATURES.search(head)
            if match:
                return f"{name} is a {match.group(0)} rip log"
    return None


def _gather(path: str) -> dict:
    """Collect every signal in one pass over the album."""
    audio = get_audio_files(path, True)
    blobs, tracknos, precisions, rates = [], [], set(), set()
    for filename in audio:
        mut = MutagenFile(os.path.join(path, filename))
        if mut is None:
            continue
        blob = _tag_blob(mut)
        blobs.append(blob)
        match = re.search(r"(?:^|\n)tracknumber=\[?'?([a-z0-9]+)", blob)
        if match:
            tracknos.append(match.group(1))
        precisions.add(getattr(mut.info, "bits_per_sample", None))
        rates.add(getattr(mut.info, "sample_rate", None))
    return {
        "audio": audio,
        "blob": "\n".join(blobs),
        "tracknos": tracknos,
        "max_precision": max((p for p in precisions if p), default=None),
        "max_rate": max((r for r in rates if r), default=None),
        "has_cue": any(f.lower().endswith(".cue") for f in os.listdir(path)),
        "rip_log": _has_rip_log(path),
    }


def _vinyl_sides(tracknos: list[str]) -> bool:
    """True when most track numbers look like vinyl sides (A1, B2 …)."""
    if len(tracknos) < 2:
        return False
    return sum(bool(_VINYL_TRACKNO.match(t)) for t in tracknos) >= len(tracknos) * 0.8


def detect_source(path: str) -> dict:
    """Return {source, confidence, reasons} for an album folder.

    confidence is "confirmed" (evidence is conclusive), "likely" (strong but
    circumstantial) or "unknown" (undecidable from the files alone).
    """
    ev = _gather(path)
    if not ev["audio"]:
        return {"source": None, "confidence": "unknown", "reasons": ["No audio files found."]}

    media = _MEDIA_TAG.search(ev["blob"])
    if media and (declared := _MEDIA_VALUES.get(media.group(1).strip())):
        return {
            "source": declared,
            "confidence": "confirmed",
            "reasons": [f'Files declare media "{media.group(1).strip()}".'],
        }

    if ev["rip_log"]:
        return {"source": "CD", "confidence": "confirmed", "reasons": [ev["rip_log"].capitalize() + "."]}

    for pattern, why in _STORE_TAGS:
        if pattern.search(ev["blob"]):
            return {
                "source": "WEB",
                "confidence": "confirmed",
                "reasons": [f"{why} — only a digital store writes this."],
            }

    if _vinyl_sides(ev["tracknos"]):
        return {"source": "Vinyl", "confidence": "likely", "reasons": ["Track numbers are vinyl sides (A1, B2 …)."]}

    # Above CD's 16/44.1 ceiling, so it cannot be a CD rip — but vinyl and SACD
    # rips are hi-res too, hence "likely" rather than proof of WEB.
    hi_res = (ev["max_precision"] or 16) > 16 or (ev["max_rate"] or 44100) > 44100
    if hi_res:
        spec = f"{ev['max_precision'] or 16}bit/{(ev['max_rate'] or 0) / 1000:g}kHz"
        return {
            "source": "WEB",
            "confidence": "likely",
            "reasons": [
                f"{spec} exceeds CD's 16bit/44.1kHz, so this is not a CD rip.",
                "Check it is not a vinyl or SACD rip.",
            ],
        }

    reasons = ["No rip log, no store tags, and 16bit/44.1kHz fits both CD and WEB."]
    if ev["has_cue"]:
        reasons.append("A cue sheet is present, which leans CD, but cue sheets are often bundled with WEB rips too.")
    reasons.append("Set the source yourself — an unverified guess would be a mislabelled upload.")
    return {"source": None, "confidence": "unknown", "reasons": reasons}
