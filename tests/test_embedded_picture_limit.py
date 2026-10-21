"""Embedded pictures and padding stay under RED's 1 MiB trump threshold, and the rules say so when they do not."""

from types import SimpleNamespace

from mutagen.flac import Picture
from mutagen.id3 import PictureType

from salmon.checks.tag_rules import collect_upload_warnings
from salmon.constants import TAG_TRUMP_SIZE
from salmon.converter import transcoding
from salmon.tagger.audio_info import metadata_size

MIB = 1024 * 1024


def _flac_like(picture_bytes: int, padding: int):
    picture = Picture()
    picture.type, picture.mime, picture.data = PictureType.COVER_FRONT, "image/jpeg", b"x" * picture_bytes
    blocks = [SimpleNamespace(code=0, length=34), SimpleNamespace(code=1, length=padding)]
    return SimpleNamespace(pictures=[picture], metadata_blocks=blocks)


def test_metadata_size_counts_pictures_and_padding_for_flac() -> None:
    # The PICTURE block's own fields (32 bytes and the MIME type) count, as upstream's #522 measures.
    assert metadata_size(_flac_like(1000, 8192)) == 1000 + 32 + len("image/jpeg") + 8192


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


def test_a_refreshed_track_keeps_its_tag_entry(monkeypatch) -> None:
    # generate_description reads track["t"]; re-reading only the audio info would drop it.
    import salmon.uploader as uploader

    monkeypatch.setattr(uploader, "gather_audio_info", lambda path: {"01.flac": {"tag size": 8192, "precision": 16}})

    refreshed = uploader.refresh_track_data("/music/album", {"01.flac": "the tags"})

    assert refreshed == {"01.flac": {"tag size": 8192, "precision": 16, "t": "the tags"}}
