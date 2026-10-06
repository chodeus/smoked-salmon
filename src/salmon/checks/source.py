"""Infer an album's media source from its own files, and read the URLs their tags carry."""

import os
import re
from dataclasses import dataclass

from mutagen import File as MutagenFile
from mutagen.id3 import TextFrame
from mutagen.mp4 import AtomDataType, MP4FreeForm

from salmon.common.files import get_audio_files

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


def field_name(key) -> str:
    """Lowercase Vorbis-style name for a tag key from any container (TXXX:SOURCE, COMM::eng, ----:…:MEDIA)."""
    name = str(key).lower()
    if name.startswith(("txxx:", "wxxx:")):
        return name[5:]
    if name.startswith("----:"):
        return name.rsplit(":", 1)[-1]
    return _FRAME_FIELDS.get(name.split(":", 1)[0], name)


def _decode_bytes(item: bytes) -> str:
    """Decode a raw tag value: UTF-16 MP4 freeform by its BOM (big-endian without one), else UTF-8."""
    if isinstance(item, MP4FreeForm) and item.dataformat == AtomDataType.UTF16:
        encoding = "utf-16" if item[:2] in (b"\xfe\xff", b"\xff\xfe") else "utf-16-be"
        return item.decode(encoding, "ignore")
    return item.decode("utf-8", "ignore")


def tag_texts(value) -> list[str]:
    """A tag value as plain strings: a list, each value of an ID3 frame, or MP4 freeform bytes."""
    if isinstance(value, TextFrame):
        # mutagen sets a frame's attributes from its spec at runtime, so its types do not know `text`.
        value = getattr(value, "text", [])
    items = value if isinstance(value, list) else [value]
    return [(_decode_bytes(item) if isinstance(item, bytes) else str(item)).strip() for item in items]


def tag_pairs(mut) -> list:
    """The file's (key, value) tag pairs, database links left out."""
    pairs = dict(mut.tags or {}).items()
    return [(key, value) for key, value in pairs if not field_name(key).startswith(_DATABASE_KEYS)]


def tag_url_fields(mut) -> list[tuple[str, str]]:
    """(field name, URL) for every tag whose whole value is one URL."""
    return [
        (field_name(key), text)
        for key, value in tag_pairs(mut)
        for text in tag_texts(value)
        if _URL_VALUE.fullmatch(text) and not _DATABASE_URL.match(text)
    ]


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


# Enough of a log to hold the ripper's banner.
_LOG_HEAD_BYTES = 4096
# CD rippers only: a verification log (CUETools, an AccurateRip check) can be made from any files.
_RIPPERS = re.compile(r"exact audio copy|x lossless decoder|whipper|morituri|dbpoweramp|cueripper", re.IGNORECASE)
# Fields only a store writes. Not ASIN (Picard copies it from MusicBrainz), and not the
# com.apple.iTunes namespace as such (every tagger files custom M4A fields under it).
_ITUNES_PURCHASE_FIELDS = frozenset({"apid", "purd", "purchase date", "purchase_date", "purchasedate"})
# The comment Bandcamp writes into its downloads.
_BANDCAMP_COMMENT = re.compile(r"visit https?://[a-z0-9-]+\.bandcamp\.com\b")
# Apple only by its music stores: the rest of apple.com sells no albums.
_STORE_URL = re.compile(
    r"https?://(?:(?:[a-z0-9-]+\.)*(qobuz|deezer|tidal|bandcamp|beatport|7digital|hdtracks)|(?:music|itunes)\.(apple))"
    r"\.com(?:[/:?#]|$)",
    re.IGNORECASE,
)
_STORE_NAMES = {
    "qobuz": "Qobuz",
    "deezer": "Deezer",
    "tidal": "Tidal",
    "bandcamp": "Bandcamp",
    "beatport": "Beatport",
    "7digital": "7digital",
    "hdtracks": "HDtracks",
    "apple": "Apple",
}
_MEDIA_FIELDS = frozenset({"media", "sourcemedia", "tmed"})
# A media tag's whole value, so that "CD/Vinyl" names no source.
_MEDIA_VALUES = {
    "cd": "CD",
    "compact disc": "CD",
    "web": "WEB",
    "digital media": "WEB",
    "file": "WEB",
    "digital": "WEB",
    "vinyl": "Vinyl",
    "lp": "Vinyl",
    '7" vinyl': "Vinyl",
    '10" vinyl': "Vinyl",
    '12" vinyl': "Vinyl",
    "cassette": "Cassette",
    "sacd": "SACD",
    "dvd": "DVD",
}
_VINYL_SIDE = re.compile(r"[A-H][0-9]{1,2}", re.IGNORECASE)
# What narrowing evidence leaves possible. Side numbering is also kept by the WEB release of a vinyl
# album, and a cassette has sides too.
_VINYL_SIDE_SOURCES = frozenset({"Vinyl", "Cassette", "WEB"})


def is_store_url(url: str) -> bool:
    """Whether the URL is a page of a store that sells downloads (Qobuz, Deezer, Bandcamp, Apple Music, ...)."""
    return bool(_STORE_URL.match(url))


@dataclass
class _Evidence:
    proofs: dict[str, str]
    """Reason -> the source it proves."""
    tracknumbers: list[str]
    above_cd_quality: bool


def detect_source(path: str) -> dict:
    """{source, confidence, reasons}: "confirmed" if proven and uncontradicted, "likely" for a hint, else "unknown"."""
    if not get_audio_files(path):
        return _unknown(["No audio files found."])
    evidence = _gather(path)
    sources = set(evidence.proofs.values())
    sides = _vinyl_sides(evidence.tracknumbers)
    proofs = "; ".join(evidence.proofs)
    if len(sources) > 1:
        named = "; ".join(f"{why} ({source})" for why, source in evidence.proofs.items())
        return _unknown([f"The files disagree: {named}."])
    if sources:
        source = sources.pop()
        if evidence.above_cd_quality and source == "CD":
            return _unknown([f"Not confirmed: {proofs}, but the files are above CD's 16bit/44.1kHz."])
        if sides and source not in _VINYL_SIDE_SOURCES:
            return _unknown([f"Not confirmed: {proofs}, but the track numbers are vinyl sides (A1, B2 ...)."])
        return {"source": source, "confidence": "confirmed", "reasons": [f"{proofs}."]}
    if sides:
        return {
            "source": "Vinyl",
            "confidence": "likely",
            "reasons": ["Track numbers are vinyl sides (A1, B2 ...), which a cassette or a WEB release can keep too."],
        }
    if evidence.above_cd_quality:
        return {
            "source": "WEB",
            "confidence": "likely",
            "reasons": ["Above CD's 16bit/44.1kHz, so this is not a CD rip.", "Check it is not a vinyl or SACD rip."],
        }
    reasons = ["No rip log, no store tags, and 16bit/44.1kHz fits both CD and WEB."]
    if _has_cue(path):
        reasons.append("A cue sheet is present, which leans CD, but cue sheets are often bundled with WEB rips too.")
    reasons.append("Set the source yourself: an unverified guess would be a mislabelled upload.")
    return _unknown(reasons)


def _unknown(reasons: list[str]) -> dict:
    return {"source": None, "confidence": "unknown", "reasons": reasons}


def _gather(path: str) -> _Evidence:
    evidence = _Evidence(proofs={}, tracknumbers=[], above_cd_quality=False)
    if log := _rip_log(path):
        evidence.proofs[f"rip log found ({log})"] = "CD"
    for filename in get_audio_files(path):
        try:
            mut = MutagenFile(os.path.join(path, filename))
        except Exception:  # A truncated or corrupt file must not sink the whole scan.
            continue
        if mut is None:
            continue
        evidence.proofs.update(_tag_proofs(mut))
        evidence.tracknumbers.extend(_tracknumbers(mut))
        bits = getattr(mut.info, "bits_per_sample", None) or 0
        rate = getattr(mut.info, "sample_rate", None) or 0
        evidence.above_cd_quality |= bits > 16 or rate > 44100
    return evidence


def _rip_log(path: str) -> str | None:
    """The name of the first CD ripper's log in the folder, if there is one."""
    for root, _dirs, files in sorted(os.walk(path)):
        for name in sorted(files):
            if not name.lower().endswith(".log"):
                continue
            try:
                with open(os.path.join(root, name), "rb") as fh:
                    head = fh.read(_LOG_HEAD_BYTES)
            except OSError:
                continue
            # EAC writes its logs in UTF-16 with a byte order mark.
            encoding = "utf-16" if head[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
            if _RIPPERS.search(head.decode(encoding, "ignore")):
                return name
    return None


def _tag_proofs(mut) -> dict[str, str]:
    """Reason -> source, for what one file's tags prove."""
    proofs = {}
    for key, value in tag_pairs(mut):
        field = field_name(key)
        for text in tag_texts(value):
            lowered = text.lower()
            if field in _MEDIA_FIELDS and (media := _MEDIA_VALUES.get(lowered)):
                proofs[f'media tag says "{text}"'] = media
            elif field in _ITUNES_PURCHASE_FIELDS:
                proofs["iTunes purchase tags"] = "WEB"
            elif "amazon.com song id" in lowered:
                proofs["Amazon download comment in the tags"] = "WEB"
            elif field == "comment" and _BANDCAMP_COMMENT.match(lowered):
                proofs["Bandcamp comment in the tags"] = "WEB"
    for _field, url in tag_url_fields(mut):
        if match := _STORE_URL.match(url):
            store = (match.group(1) or match.group(2)).lower()
            proofs[f"{_STORE_NAMES[store]} URL in the tags"] = "WEB"
    return proofs


def _tracknumbers(mut) -> list[str]:
    return [
        text.split("/", 1)[0].strip()
        for key, value in tag_pairs(mut)
        if field_name(key) in ("tracknumber", "trck")
        for text in tag_texts(value)
    ]


def _vinyl_sides(tracknumbers: list[str]) -> bool:
    """True when most track numbers are vinyl sides (A1, B2 ...)."""
    if len(tracknumbers) < 2:
        return False
    return sum(bool(_VINYL_SIDE.fullmatch(t)) for t in tracknumbers) >= len(tracknumbers) * 0.8


def _has_cue(path: str) -> bool:
    """Cue sheets are often one level down, beside the audio rather than at the root."""
    return any(name.lower().endswith(".cue") for _root, _dirs, files in os.walk(path) for name in files)
