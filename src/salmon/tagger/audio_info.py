import os

import asyncclick as click
from mutagen import File as MutagenFile

from salmon.common import compress, get_audio_files
from salmon.errors import UploadError


def gather_audio_info(path, sort_by_tracknumber=False):
    """
    Iterate over all audio files in the directory and parse the technical
    information about the files into a dictionary.
    """
    files = get_audio_files(path, sort_by_tracknumber)
    if not files:
        raise UploadError("No audio files found.")

    audio_info = {}
    for filename in files:
        mut = MutagenFile(os.path.join(path, filename))
        if mut is None:
            raise UploadError(f"Could not read audio file: {filename}")
        filepath = os.path.join(path, filename)
        audio_info[filename] = {
            **_parse_audio_info(mut.info),
            "tag size": metadata_size(mut),
            "id3": has_id3_tag(filepath),
        }
    return audio_info


def has_id3_tag(filepath: str) -> bool:
    """ID3v2 at the start or ID3v1 at the end of the file: normal in an MP3, a trump reason inside a FLAC."""
    with open(filepath, "rb") as handle:
        if handle.read(3) == b"ID3":
            return True
        handle.seek(0, os.SEEK_END)
        if handle.tell() < 128:
            return False
        handle.seek(-128, os.SEEK_END)
        return handle.read(3) == b"TAG"


def metadata_size(mut) -> int | None:
    """Bytes spent on pictures and padding (FLAC) or on the ID3 tag (MP3); None when the format has no such measure."""
    pictures = getattr(mut, "pictures", None)
    blocks = getattr(mut, "metadata_blocks", None)
    if pictures is not None and blocks is not None:
        return sum(len(picture.data) for picture in pictures) + sum(block.length for block in blocks if block.code == 1)
    size = getattr(getattr(mut, "tags", None), "size", None)
    return size if isinstance(size, int) else None


def _parse_audio_info(streaminfo):
    return {
        "channels": streaminfo.channels,
        "sample rate": streaminfo.sample_rate,
        "bit rate": streaminfo.bitrate,
        "precision": getattr(streaminfo, "bits_per_sample", None),
        "duration": int(streaminfo.length),
    }


def check_hybrid(tags):
    """Check whether or not the release has mixed precisions/sample rate."""
    first_tag = next(iter(tags.values()))
    if not all(
        t["precision"] == first_tag["precision"] and t["sample rate"] == first_tag["sample rate"] for t in tags.values()
    ):
        click.secho(
            "Release has mixed bit depths / sample rates. Flagging as hybrid.",
            fg="yellow",
        )
        return True
    return False


async def recompress_path(path: str) -> None:
    """Recompress all flacs in the directory to the configured compression level.

    Args:
        path: Path to the directory containing FLAC files.
    """
    files = get_audio_files(path)
    if not files or not all(".flac" in f for f in files):
        return click.secho("No flacs found to recompress. Skipping...", fg="red")
    for filename in files:
        filepath = os.path.join(path, filename)
        await compress(filepath)
