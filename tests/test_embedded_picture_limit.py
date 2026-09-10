"""Embedded pictures and padding stay under RED's 1 MiB trump threshold, and the rules say so when they do not."""

from types import SimpleNamespace

from mutagen.id3 import PictureType

from salmon.checks.tag_rules import collect_upload_warnings
from salmon.constants import TAG_TRUMP_SIZE
from salmon.converter import transcoding
from salmon.tagger import cover
from salmon.tagger.audio_info import metadata_size

MIB = 1024 * 1024


def _flac_like(picture_bytes: int, padding: int):
    pictures = [SimpleNamespace(type=PictureType.COVER_FRONT, mime="image/jpeg", data=b"x" * picture_bytes)]
    blocks = [SimpleNamespace(code=0, length=34), SimpleNamespace(code=1, length=padding)]
    return SimpleNamespace(pictures=pictures, metadata_blocks=blocks)


def test_metadata_size_counts_pictures_and_padding_for_flac() -> None:
    assert metadata_size(_flac_like(1000, 8192)) == 9192


def test_metadata_size_uses_the_id3_tag_size_for_mp3() -> None:
    assert metadata_size(SimpleNamespace(tags=SimpleNamespace(size=2048))) == 2048


def test_metadata_size_is_unknown_for_other_formats() -> None:
    assert metadata_size(SimpleNamespace(tags=SimpleNamespace())) is None


def test_rules_flag_a_tag_block_over_one_mib() -> None:
    tracks = {"01. Song.flac": {"sample rate": 44100, "precision": 16, "tag size": MIB + 1}}

    warnings = collect_upload_warnings("RED", "Artist - Album (2020) [WEB FLAC]", tracks)

    assert warnings == ["1024 KiB of embedded pictures and padding exceeds 1 MiB (a trump reason): 01. Song.flac"]


def test_rules_allow_a_tag_block_of_exactly_one_mib() -> None:
    tracks = {"01. Song.flac": {"sample rate": 44100, "precision": 16, "tag size": MIB}}

    assert collect_upload_warnings("RED", "Artist - Album (2020) [WEB FLAC]", tracks) == []


class _FakeFLAC:
    instances: list = []
    picture_bytes = 0
    padding = 8192

    def __init__(self, _path):
        front = SimpleNamespace(type=PictureType.COVER_FRONT, mime="image/jpeg", data=b"x" * self.picture_bytes)
        self.pictures = [front]
        self.metadata_blocks = [SimpleNamespace(code=1, length=self.padding)]
        self.saved_with: list = []
        _FakeFLAC.instances.append(self)

    def clear_pictures(self):
        self.pictures = []

    def save(self, padding=None):
        self.saved_with.append(padding)


def _album_with_flac(monkeypatch, picture_bytes: int) -> dict:
    _FakeFLAC.instances = []
    monkeypatch.setattr(_FakeFLAC, "picture_bytes", picture_bytes)
    monkeypatch.setattr(cover, "FLAC", _FakeFLAC)
    monkeypatch.setattr(cover.cfg.upload.formatting, "lowercase_cover", True)
    return {"01. Song.flac": {"tag size": picture_bytes + 8192}}


def test_strip_removes_oversized_pictures_and_keeps_the_front_cover(album_dir, monkeypatch) -> None:
    track_data = _album_with_flac(monkeypatch, 2 * MIB)

    cover.strip_oversized_pictures(str(album_dir), track_data)

    flac = _FakeFLAC.instances[0]
    assert flac.pictures == []
    assert flac.saved_with == [cover.get_8kib_padding]
    assert (album_dir / "cover.jpg").stat().st_size == 2 * MIB


def test_strip_leaves_pictures_under_the_threshold_alone(album_dir, monkeypatch) -> None:
    track_data = _album_with_flac(monkeypatch, 900 * 1024)

    cover.strip_oversized_pictures(str(album_dir), track_data)

    assert _FakeFLAC.instances == []
    assert not (album_dir / "cover.jpg").exists()


def test_strip_skips_a_file_it_cannot_read(album_dir, monkeypatch) -> None:
    def broken(_path):
        raise OSError("file said 2 bytes, read 0 bytes")

    monkeypatch.setattr(cover, "FLAC", broken)

    cover.strip_oversized_pictures(str(album_dir), {"01. Song.flac": {"tag size": 2 * MIB}})

    assert not (album_dir / "cover.jpg").exists()


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
    assert TAG_TRUMP_SIZE == MIB
