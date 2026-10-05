"""Transcode refusals, and what they must not destroy on the way out."""

import os
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from salmon.converter.transcoding import TranscodeItem, _build_output_path, transcode_folder


def _streaminfo_only_flac() -> bytes:
    """A FLAC of 16bit/44.1kHz with no audio: the output path reads the source's resolution."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    return b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo


_FLAC = _streaminfo_only_flac()


def _multichannel_item(src: str, dst: str) -> TranscodeItem:
    """A stand-in for a 5.1 source; only info.channels is read on this path."""
    fake: Any = SimpleNamespace(info=SimpleNamespace(channels=6))
    return TranscodeItem(src=src, dst=dst, tags={}, flac_obj=fake)


async def test_a_multichannel_source_is_refused_before_a_prior_output_is_deleted(tmp_path, monkeypatch):
    """The refusal used to run after the overwrite branch, so an unusable source
    cost you the transcode that was already sitting there."""
    source = tmp_path / "Artist - Album (2024) [FLAC]"
    source.mkdir()
    (source / "01.flac").write_bytes(_FLAC)

    existing = tmp_path / os.path.basename(_build_output_path(str(source), "320"))
    existing.mkdir()
    keepsake = existing / "01 - already here.mp3"
    keepsake.write_bytes(b"prior output")

    monkeypatch.setattr(
        "salmon.converter.transcoding._collect_transcode_items",
        lambda _p, new_path: [_multichannel_item(str(source / "01.flac"), f"{new_path}/01.mp3")],
    )

    with pytest.raises(ValueError, match="channels"):
        await transcode_folder(str(source), "320")

    assert keepsake.exists(), "the previous output was deleted for a source that could never transcode"
    assert keepsake.read_bytes() == b"prior output"


async def test_a_multichannel_source_writes_no_destination_at_all(tmp_path, monkeypatch):
    source = tmp_path / "Artist - Other (2024) [FLAC]"
    source.mkdir()
    (source / "01.flac").write_bytes(_FLAC)
    (source / "cover.jpg").write_bytes(b"jpg")

    monkeypatch.setattr(
        "salmon.converter.transcoding._collect_transcode_items",
        lambda _p, new_path: [_multichannel_item(str(source / "01.flac"), f"{new_path}/01.mp3")],
    )

    with pytest.raises(ValueError, match="channels"):
        await transcode_folder(str(source), "320")

    # Extras are copied before encoding, so a late refusal left a folder behind.
    assert not os.path.isdir(_build_output_path(str(source), "320"))
