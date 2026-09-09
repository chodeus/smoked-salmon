"""A converted folder remembers where it came from, so uploading it later describes the transcode."""

import anyio

import salmon.uploader as uploader
from salmon.converter import conversions
from salmon.converter import downconverting as dc
from salmon.converter import transcoding as tc


def test_record_and_lookup_roundtrip(tmp_path) -> None:
    out = tmp_path / "Artist - Album (2022) [WEB FLAC]"
    out.mkdir()

    conversions.record_conversion(str(out), source="/x/src", kind="downconvert", bit_depth=16, sample_rate=44100)

    assert conversions.conversion_of(str(out)) == {
        "source": "/x/src",
        "kind": "downconvert",
        "bit_depth": 16,
        "sample_rate": 44100,
    }
    assert conversions.conversion_of(str(tmp_path / "unrelated")) is None
    assert (tmp_path / conversions.REGISTRY).exists()
    assert not (out / conversions.REGISTRY).exists(), "the registry never lands inside the album"


def test_a_corrupt_registry_is_ignored_and_replaced(tmp_path) -> None:
    (tmp_path / conversions.REGISTRY).write_text("{not json")
    out = tmp_path / "Album [WEB FLAC]"
    out.mkdir()

    assert conversions.conversion_of(str(out)) is None
    conversions.record_conversion(str(out), source="/x", kind="transcode", bitrate="V0")
    assert conversions.conversion_of(str(out)) == {"source": "/x", "kind": "transcode", "bitrate": "V0"}


def test_convert_folder_records_its_output(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB 24bit FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB FLAC]"

    async def no_conversion(_items, _bit_depth):
        return None

    monkeypatch.setattr(dc, "_build_output_path", lambda *_a: str(out))
    monkeypatch.setattr(dc, "_collect_convert_items", lambda *_a: [])
    monkeypatch.setattr(dc, "_copy_extra_files", lambda *_a, **_k: None)
    monkeypatch.setattr(dc, "_convert_audio_files", no_conversion)

    rate, path = anyio.run(dc.convert_folder, str(src), 16, 44100)

    assert (rate, path) == (44100, str(out))
    assert conversions.conversion_of(str(out)) == {
        "source": str(src),
        "kind": "downconvert",
        "bit_depth": 16,
        "sample_rate": 44100,
    }


def test_transcode_folder_records_its_output(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB MP3 V0]"

    async def no_transcode(_items, _bitrate):
        return None

    monkeypatch.setattr(tc, "_validate_lossless", lambda _path: None)
    monkeypatch.setattr(tc, "_build_output_path", lambda *_a: str(out))
    monkeypatch.setattr(tc, "_collect_transcode_items", lambda *_a: [])
    monkeypatch.setattr(tc, "_validate_channel_count", lambda _items: None)
    monkeypatch.setattr(tc, "_copy_extra_files", lambda *_a, **_k: None)
    monkeypatch.setattr(tc, "_transcode_audio_files", no_transcode)

    assert anyio.run(tc.transcode_folder, str(src), "V0") == str(out)
    assert conversions.conversion_of(str(out)) == {"source": str(src), "kind": "transcode", "bitrate": "V0"}


def test_conversion_description_uses_the_in_run_wording() -> None:
    url = "https://redacted.sh/torrents.php?id=2855221"
    downconvert = {"source": "/x", "kind": "downconvert", "bit_depth": 16, "sample_rate": 44100}
    transcode = {"source": "/x", "kind": "transcode", "bitrate": "V0"}

    assert uploader.conversion_description(downconvert, url) == dc.generate_conversion_description(url, 44100, 16)
    assert uploader.conversion_description(transcode, url) == tc.generate_transcode_description(url, "V0")
    assert uploader.conversion_description(None, url) is None
