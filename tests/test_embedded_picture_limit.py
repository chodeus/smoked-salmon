"""Embedded pictures and padding stay under RED's 1 MiB trump threshold, and the rules say so when they do not."""

from pathlib import Path
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

    assert warnings == [
        f"{MIB + 1} bytes of embedded tag exceeds the {MIB}-byte limit (2.3.19, a trump reason): 01. Song.flac"
    ]


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

    stripped = cover.strip_oversized_pictures(str(album_dir), track_data)

    flac = _FakeFLAC.instances[0]
    assert stripped == ["01. Song.flac"]
    assert flac.pictures == []
    assert flac.saved_with == [cover.get_8kib_padding]
    assert (album_dir / "cover.jpg").stat().st_size == 2 * MIB


def test_strip_leaves_pictures_under_the_threshold_alone(album_dir, monkeypatch) -> None:
    track_data = _album_with_flac(monkeypatch, 900 * 1024)

    stripped = cover.strip_oversized_pictures(str(album_dir), track_data)

    assert stripped == []
    assert _FakeFLAC.instances == []
    assert not (album_dir / "cover.jpg").exists()


def test_strip_skips_a_file_it_cannot_read(album_dir, monkeypatch) -> None:
    opened: list[str] = []

    def broken(path):
        opened.append(path)
        raise OSError("file said 2 bytes, read 0 bytes")

    monkeypatch.setattr(cover, "FLAC", broken)

    stripped = cover.strip_oversized_pictures(str(album_dir), {"01. Song.flac": {"tag size": 2 * MIB}})

    assert [Path(path).name for path in opened] == ["01. Song.flac"], "the oversized file must be opened"
    assert stripped == []
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


def test_a_failed_compression_is_not_embedded(album_dir, monkeypatch) -> None:
    # compress_to_target_size returns None when quality 75 is still too big; Picture.data must not become None.
    saved: list = []

    class _NoPictures:
        pictures: list = []

        def __init__(self, _path):
            pass

        def add_picture(self, picture):
            saved.append(picture)

        def save(self, padding=None):
            saved.append("saved")

    monkeypatch.setattr(cover, "get_audio_files", lambda path, *a, **k: ["01. Song.flac"])
    monkeypatch.setattr(cover, "FLAC", _NoPictures)
    monkeypatch.setattr(cover, "compress_to_target_size", lambda *_a: None)
    monkeypatch.setattr(cover.Image, "open", lambda *_a: SimpleNamespace(thumbnail=lambda *_x: None))
    said: list[str] = []
    monkeypatch.setattr(cover.click, "secho", lambda message, **_kwargs: said.append(str(message)))
    (album_dir / "cover.jpg").write_bytes(b"x" * (2 * MIB))

    cover.compress_pictures(str(album_dir))

    assert saved == [], "nothing may be embedded or saved when the cover could not be shrunk"
    assert any("leaving it unembedded" in message for message in said)


def test_a_refreshed_track_keeps_its_tag_entry(monkeypatch) -> None:
    # generate_description reads track["t"]; re-reading only the audio info would drop it.
    import salmon.uploader as uploader

    monkeypatch.setattr(uploader, "gather_audio_info", lambda path: {"01.flac": {"tag size": 8192, "precision": 16}})

    refreshed = uploader.refresh_track_data("/music/album", {"01.flac": "the tags"})

    assert refreshed == {"01.flac": {"tag size": 8192, "precision": 16, "t": "the tags"}}
