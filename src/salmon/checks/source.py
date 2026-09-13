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
# Tags only a digital storefront writes. Not ASIN (Picard copies it from MusicBrainz) and not the
# com.apple.iTunes atom namespace (every tagger files custom M4A fields under it).
_STORE_TAGS = (
    (re.compile(r"amazon\.com song id"), "Amazon download comment"),
    (re.compile(r"(^|\n)(apid|purd)=|(^|\n)purchase[ _]?date="), "iTunes purchase tags"),
    (re.compile(r"bandcamp\.com"), "Bandcamp tag"),
)
_STORE_URL = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*(?:qobuz|deezer|tidal|bandcamp|beatport|7digital|hdtracks|apple)\.com(?:[/:?#]|$)",
    re.IGNORECASE,
)
# Keys downloaders use for the page the files came from; WOAS is ID3's "official audio source".
_SOURCE_KEYS = frozenset({"source", "sourceurl", "www", "website", "url", "purl", "woas"})
# MusicBrainz and Discogs links describe a release in a database, not where these files came from:
# skip both their keys (which can hold a store link) and their own URLs under any key.
_DATABASE_KEYS = ("musicbrainz", "discogs")
_DATABASE_URL = re.compile(r"https?://(?:[a-z0-9-]+\.)*(?:musicbrainz\.org|discogs\.com)(?:[/:?#]|$)", re.IGNORECASE)
_URL_VALUE = re.compile(r"https?://\S+", re.IGNORECASE)
# ID3 and MP4 frames that hold a field under another name.
_FRAME_FIELDS = {
    "comm": "comment",
    "tenc": "encoded-by",
    "tsse": "encoder settings",
    "\xa9cmt": "comment",
    "\xa9too": "encoder",
}
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


def field_name(key) -> str:
    """Lowercase Vorbis-style name for a tag key from any container (TXXX:SOURCE, COMM::eng, ----:…:MEDIA)."""
    name = str(key).lower()
    if name.startswith(("txxx:", "wxxx:")):
        return name[5:]
    if name.startswith("----:"):
        return name.rsplit(":", 1)[-1]
    return _FRAME_FIELDS.get(name.split(":", 1)[0], name)


def tag_texts(value) -> list[str]:
    """A tag value as plain strings, whether a list, an ID3 frame or MP4 freeform bytes."""
    items = value if isinstance(value, list) else [value]
    return [(item.decode("utf-8", "ignore") if isinstance(item, bytes) else str(item)).strip() for item in items]


def _tags(mut) -> list:
    """The file's (key, value) tag pairs, database links left out."""
    pairs = dict(mut.tags or {}).items()
    return [(key, value) for key, value in pairs if not field_name(key).startswith(_DATABASE_KEYS)]


def _tag_blob(mut) -> str:
    """Flatten one file's tags to lowercase `field=value` lines, format-agnostic."""
    return "\n".join(f"{field_name(key)}={'; '.join(tag_texts(value))}".lower() for key, value in _tags(mut))


def tag_url_fields(mut) -> list[tuple[str, str]]:
    """(field name, URL) for every tag whose whole value is one URL."""
    return [
        (field_name(key), text)
        for key, value in _tags(mut)
        for text in tag_texts(value)
        if _URL_VALUE.fullmatch(text) and not _DATABASE_URL.match(text)
    ]


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


def _has_cue(path: str) -> bool:
    """Cue sheets are often one level down, beside the audio rather than at the root."""
    return any(name.lower().endswith(".cue") for _root, _dirs, files in os.walk(path) for name in files)


def _gather(path: str) -> dict:
    """Collect every signal in one pass over the album."""
    audio = get_audio_files(path, True)
    blobs, urls, tracknos, precisions, rates = [], [], [], set(), set()
    for filename in audio:
        try:
            mut = MutagenFile(os.path.join(path, filename))
        except Exception:  # a truncated or corrupt file must not sink the whole scan
            continue
        if mut is None:
            continue
        blob = _tag_blob(mut)
        blobs.append(blob)
        urls.extend(url for _field, url in tag_url_fields(mut))
        match = re.search(r"(?:^|\n)tracknumber=\[?'?([a-z0-9]+)", blob)
        if match:
            tracknos.append(match.group(1))
        precisions.add(getattr(mut.info, "bits_per_sample", None))
        rates.add(getattr(mut.info, "sample_rate", None))
    return {
        "audio": audio,
        "blob": "\n".join(blobs),
        "urls": urls,
        "tracknos": tracknos,
        "max_precision": max((p for p in precisions if p), default=None),
        "max_rate": max((r for r in rates if r), default=None),
        "has_cue": _has_cue(path),
        "rip_log": _has_rip_log(path),
    }


def _vinyl_sides(tracknos: list[str]) -> bool:
    """True when most track numbers look like vinyl sides (A1, B2 …)."""
    if len(tracknos) < 2:
        return False
    return sum(bool(_VINYL_TRACKNO.match(t)) for t in tracknos) >= len(tracknos) * 0.8


def tag_urls(path: str) -> tuple[list[str], list[str]]:
    """URLs the files' tags hold whole: (under a source key such as SOURCE or WOAS, under any other key)."""
    sourced, other = [], []
    for filename in get_audio_files(path, True):
        try:
            mut = MutagenFile(os.path.join(path, filename))
        except Exception:
            continue
        if mut is None:
            continue
        for field, url in tag_url_fields(mut):
            (sourced if field in _SOURCE_KEYS else other).append(url)
    return list(dict.fromkeys(sourced)), list(dict.fromkeys(other))


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

    if any(_STORE_URL.match(url) for url in ev["urls"]):
        return {
            "source": "WEB",
            "confidence": "confirmed",
            "reasons": ["Store URL tag — only a digital store writes this."],
        }

    if _vinyl_sides(ev["tracknos"]):
        return {"source": "Vinyl", "confidence": "likely", "reasons": ["Track numbers are vinyl sides (A1, B2 …)."]}

    # Above CD's 16/44.1 ceiling, so it cannot be a CD rip — but vinyl and SACD
    # rips are hi-res too, hence "likely" rather than proof of WEB.
    hi_res = (ev["max_precision"] or 16) > 16 or (ev["max_rate"] or 44100) > 44100
    if hi_res:
        rate = f"{ev['max_rate'] / 1000:g}kHz" if ev["max_rate"] else "an unknown rate"
        spec = f"{ev['max_precision'] or 16}bit/{rate}"
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
