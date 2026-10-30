"""What the MP3 encoding step writes, and that a cancel stops it."""

import asyncio
from types import SimpleNamespace

import anyio
import pytest
from mutagen.id3 import PictureType

from salmon.converter import transcoding
from salmon.tagger.cover import MAX_PICTURES_AND_PADDING

MIB = 1024 * 1024


def test_transcode_embeds_only_the_pictures_that_fit(monkeypatch) -> None:
    added: list = []
    fake_mp3 = SimpleNamespace(tags=SimpleNamespace(add=added.append), save=lambda **kwargs: None)
    monkeypatch.setattr(transcoding.mp3, "MP3", lambda _path: fake_mp3)
    pictures = [
        SimpleNamespace(mime="image/jpeg", type=PictureType.COVER_FRONT, desc="", data=b"x" * (2 * MIB)),
        SimpleNamespace(mime="image/jpeg", type=PictureType.COVER_BACK, desc="", data=b"y" * (500 * 1024)),
    ]
    flac_obj = SimpleNamespace(pictures=pictures)

    transcoding._copy_tags({}, flac_obj, "/tmp/out.mp3")  # type: ignore[arg-type]

    assert [len(frame.data) for frame in added] == [500 * 1024]
    assert MAX_PICTURES_AND_PADDING == MIB


def test_a_cancelled_transcode_kills_flac_and_lame(monkeypatch, tmp_path) -> None:
    spawned: list = []

    class Encoder:
        returncode = None

        def __init__(self) -> None:
            self.killed = False

        async def communicate(self):
            raise asyncio.CancelledError

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            return -9

    async def spawn(*_args, **_kwargs) -> Encoder:
        spawned.append(Encoder())
        return spawned[-1]

    monkeypatch.setattr(transcoding.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(asyncio.CancelledError):
        anyio.run(transcoding._flac_to_mp3, "V0", str(tmp_path / "01.flac"), str(tmp_path / "01.mp3"))

    assert [encoder.killed for encoder in spawned] == [True, True]
