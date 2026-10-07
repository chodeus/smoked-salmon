"""Pre-upload path and sample-rate rules for RED/OPS.

Owns the path *measurement* for the whole codebase. The warnings here are advisory,
but folderstructure's blocking check measures the same way — two measurements that
disagree is how a folder passed one gate and failed the other.
"""

import os

from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import ID3

from salmon.common import get_audio_files
from salmon.constants import TAG_TRUMP_SIZE
from salmon.tagger.audio_info import has_id3_tag

# Max full in-torrent path: the top-level torrent folder, any subfolders, and the
# filename — nested folders and long classical filenames all count against it.
MAX_PATH_LENGTH = {"RED": 180, "OPS": 255}
# The folder is prepared once, before a tracker is chosen, so it has to satisfy the
# strictest destination it might go to.
STRICTEST_PATH_LENGTH = min(MAX_PATH_LENGTH.values())
STANDARD_SAMPLE_RATES = {44100, 48000, 88200, 96000, 176400, 192000}
# A FLAC storing verbatim frames reaches raw PCM; the margin allows for the frame headers' own overhead.
UNCOMPRESSED_RATIO = 0.99


def in_torrent_path(folder_name: str, relative_path: str) -> str:
    """The path a tracker counts: the torrent's top-level folder plus what sits under it.

    Not the on-disk path. Measuring from download_directory only coincides with this
    when the album happens to live there, and under-counts everywhere else.
    """
    return f"{folder_name}/{relative_path}" if relative_path not in ("", ".") else folder_name


def is_uncompressed(track: dict) -> bool:
    """True when the file's audio bit rate is within a per cent of the raw PCM rate for its format."""
    rate, bits, channels = track.get("sample rate"), track.get("precision"), track.get("channels")
    bit_rate = track.get("bit rate")
    if not (rate and bits and channels and bit_rate):
        return False
    # mutagen measures from the end of the metadata blocks, so pictures and padding are already out of it.
    return bit_rate >= UNCOMPRESSED_RATIO * rate * bits * channels


def collect_upload_warnings(site_code: str, folder_name: str, track_data: dict) -> list[str]:
    """Return human-readable rule warnings for this upload; empty when clean."""
    warnings = []
    path_limit = MAX_PATH_LENGTH.get(site_code)
    for filename, track in track_data.items():
        full_path = in_torrent_path(folder_name, filename)
        if path_limit and len(full_path) > path_limit:
            warnings.append(
                f"{len(full_path)}-char path exceeds {site_code}'s {path_limit} limit "
                f"(2.3.12, a trump reason): {full_path}"
            )
        tag_size = track.get("tag size")
        if tag_size is not None and tag_size > TAG_TRUMP_SIZE:
            warnings.append(
                f"{tag_size} bytes of embedded tag exceeds the {TAG_TRUMP_SIZE}-byte limit "
                f"(2.3.19, a trump reason): {filename}"
            )
        if filename.lower().endswith(".flac"):
            if track.get("id3"):
                warnings.append(f"ID3 tag inside a FLAC (2.2.10.8, a trump reason), left in place: {filename}")
            if is_uncompressed(track):
                warnings.append(f"Uncompressed FLAC (2.2.10.10, not allowed); recompress it (salmon up -c): {filename}")
        sample_rate = track.get("sample rate")
        precision = track.get("precision")
        if sample_rate and sample_rate not in STANDARD_SAMPLE_RATES:
            warnings.append(f"Non-standard sample rate {sample_rate} Hz may be rejected: {filename}")
        elif precision == 16 and sample_rate and sample_rate > 48000:
            # OPS forbids it outright; RED only makes it trumpable.
            if site_code == "OPS":
                warnings.append(
                    f"16-bit above 48 kHz is not permitted on OPS — downsample to 16/44.1 or 16/48: {filename}"
                )
            else:
                warnings.append(f"16-bit above 48 kHz is trumpable on RED — downsample to 16/44.1 or 16/48: {filename}")
    return warnings


def path_limit_for(site_codes) -> int:
    """The strictest in-torrent path limit among these trackers; the strictest of all for an unknown one."""
    return min((MAX_PATH_LENGTH.get(code, STRICTEST_PATH_LENGTH) for code in site_codes), default=STRICTEST_PATH_LENGTH)


def _id3v1_holds_text(filepath: str) -> bool:
    """Whether the file ends in an ID3v1 block with anything in its text fields."""
    with open(filepath, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() < 128:
            return False
        handle.seek(-128, os.SEEK_END)
        block = handle.read(128)
    # Byte 126 is the ID3v1.1 track number when byte 125 is zero, else the comment's end.
    text = block[3:125] if block[125] == 0 else block[3:127]
    return block[:3] == b"TAG" and bool(text.strip(b"\0 "))


def _has_id3v2_header(filepath: str) -> bool:
    with open(filepath, "rb") as handle:
        return handle.read(3) == b"ID3"


def has_blank_id3v2_alongside_id3v1(filepath: str) -> bool:
    """True for an MP3 whose filled-in ID3v1 tag sits beside a frameless ID3v2 tag, both found on disk."""
    if not (_id3v1_holds_text(filepath) and _has_id3v2_header(filepath)):
        return False
    try:
        # load_v1=False: mutagen otherwise reads the v1 fields into the v2 tag, and a blank v2 looks filled.
        tags = ID3(filepath, load_v1=False)
    except (MutagenError, OSError):
        return False
    return not any(tags.getall(key) for key in tags)


def process_tag_issues(path: str, *, scene: bool) -> list[str]:
    """Strip FLACs' ID3 tags (a scene release only gets a note) and note dual-ID3 MP3s; one line per file."""
    messages: list[str] = []
    for filename in get_audio_files(path):
        filepath = os.path.join(path, filename)
        lower = filename.lower()
        if lower.endswith(".flac") and has_id3_tag(filepath):
            if scene:
                messages.append(
                    f"{filename}: FLAC file contains an ID3 tag (RED and OPS do not allow ID3 tags in FLAC files)."
                )
                continue
            try:
                flac = FLAC(filepath)
                padding = sum(block.length for block in flac.metadata_blocks if block.code == 1)
                # The original padding, not the freed ID3 bytes too, which would count against the 1 MiB rule.
                flac.save(deleteid3=True, padding=lambda _info, keep=padding: keep)
            except (MutagenError, OSError) as e:
                messages.append(f"{filename}: could not remove its ID3 tag ({e}); remove it by hand.")
            else:
                messages.append(
                    f"Removed an ID3 tag from {filename} (RED and OPS do not allow ID3 tags in FLAC files)."
                )
        elif lower.endswith(".mp3") and has_blank_id3v2_alongside_id3v1(filepath):
            messages.append(
                f"{filename}: MP3 file has a filled-in ID3v1 tag and a blank ID3v2 tag (RED and OPS can trump it)."
            )
    return messages
