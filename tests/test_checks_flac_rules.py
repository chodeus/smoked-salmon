"""Rules learned from propolis: ID3 tags inside FLACs, uncompressed FLACs, odd spaces, and rule numbers."""

import pytest

from salmon.checks.tag_rules import collect_upload_warnings, is_uncompressed
from salmon.common.strings import plain_spaces
from salmon.tagger import foldername
from salmon.tagger.audio_info import has_id3_tag

FOLDER = "Artist - Album (2020) [WEB FLAC]"


def _flac(**overrides) -> dict:
    track = {
        "channels": 2,
        "sample rate": 44100,
        "bit rate": 900_000,
        "precision": 16,
        "duration": 240,
        "tag size": 8192,
        "id3": False,
    }
    track.update(overrides)
    return track


def test_has_id3_tag_sees_a_leading_id3v2_header(tmp_path) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(b"ID3\x04\x00" + b"\x00" * 300)

    assert has_id3_tag(str(path)) is True


def test_has_id3_tag_sees_a_trailing_id3v1_block(tmp_path) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(b"fLaC" + b"\x00" * 300 + b"TAG" + b"\x00" * 125)

    assert has_id3_tag(str(path)) is True


@pytest.mark.parametrize("payload", [b"fLaC" + b"\x00" * 300, b"fLaC"])
def test_has_id3_tag_is_false_for_a_clean_or_tiny_file(tmp_path, payload: bytes) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(payload)

    assert has_id3_tag(str(path)) is False


def test_uncompressed_is_the_raw_pcm_rate() -> None:
    raw = 44100 * 16 * 2

    assert is_uncompressed(_flac(**{"bit rate": raw})) is True
    assert is_uncompressed(_flac(**{"bit rate": int(raw * 0.7)})) is False


def test_uncompressed_discounts_the_tag_block_and_needs_every_fact() -> None:
    raw = 44100 * 16 * 2
    # a 1 MiB picture on a 10-second track inflates the file bit rate well past raw PCM
    picture_bits_per_second = 1024 * 1024 * 8 // 10
    picture_heavy = _flac(
        **{"bit rate": int(raw * 0.7) + picture_bits_per_second, "duration": 10, "tag size": 1024 * 1024}
    )

    assert is_uncompressed(picture_heavy) is False
    assert is_uncompressed(_flac(**{"bit rate": raw, "duration": None})) is False


def test_rules_flag_an_id3_tag_only_inside_a_flac() -> None:
    tracks = {"01. Song.flac": _flac(id3=True), "02. Song.mp3": {"id3": True, "sample rate": 44100}}

    warnings = collect_upload_warnings("RED", FOLDER, tracks)

    assert warnings == [
        "ID3 tag inside a FLAC (2.2.10.8, a trump reason); the integrity re-encode removes it: 01. Song.flac"
    ]


def test_rules_flag_an_uncompressed_flac() -> None:
    warnings = collect_upload_warnings("RED", FOLDER, {"01. Song.flac": _flac(**{"bit rate": 44100 * 16 * 2})})

    assert warnings == ["Uncompressed FLAC (2.2.10.10, not allowed); recompress it (salmon up -c): 01. Song.flac"]


def test_rules_cite_the_path_rule_number() -> None:
    warnings = collect_upload_warnings("RED", "A" * 170, {"01. Song.flac": _flac()})

    assert len(warnings) == 1
    assert "(2.3.12, a trump reason)" in warnings[0]


def test_plain_spaces_normalises_unicode_space_variants() -> None:
    assert plain_spaces("Live at the\u3000Hall") == "Live at the Hall"
    assert plain_spaces("plain text") == "plain text"


def test_folder_names_lose_odd_spaces_before_the_blacklist_pass(monkeypatch) -> None:
    monkeypatch.setattr(foldername.cfg.upload.description, "fullwidth_replacements", False)

    assert foldername._sub_illegal_characters("Live at the\u00a0Hall: Vol. 2") == "Live at the Hall_ Vol. 2"
