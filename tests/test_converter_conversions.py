"""A converted folder remembers where it came from, so uploading it later describes the conversion."""

from types import SimpleNamespace

import anyio
import pytest

import salmon.uploader as uploader
from salmon.converter import conversions
from salmon.converter import downconverting as dc
from salmon.converter import transcoding as tc
from salmon.tagger import foldername

DOWNCONVERT = {"source": "/x/src", "kind": "downconvert", "bit_depth": 16, "sample_rate": 44100}


def test_record_and_lookup_roundtrip(tmp_path) -> None:
    out = tmp_path / "Artist - Album (2022) [WEB FLAC]"
    out.mkdir()

    conversions.record_conversion(str(out), **DOWNCONVERT)

    assert conversions.conversion_of(str(out)) == DOWNCONVERT
    assert conversions.conversion_of(str(tmp_path / "unrelated")) is None
    assert (tmp_path / conversions.REGISTRY_DIR / f"{out.name}.json").exists()
    assert not any(out.iterdir()), "nothing lands inside the album"


def test_two_folders_under_one_parent_keep_both_records(tmp_path) -> None:
    first, second = tmp_path / "A [WEB FLAC]", tmp_path / "B [WEB FLAC]"

    conversions.record_conversion(str(first), **DOWNCONVERT)
    conversions.record_conversion(str(second), source="/x", kind="transcode", bitrate="V0")
    conversions.record_conversion(str(first), **{**DOWNCONVERT, "sample_rate": 48000})

    assert conversions.conversion_of(str(first)) == {**DOWNCONVERT, "sample_rate": 48000}
    assert conversions.conversion_of(str(second)) == {"source": "/x", "kind": "transcode", "bitrate": "V0"}
    assert not list((tmp_path / conversions.REGISTRY_DIR).glob("*.tmp"))


def test_a_corrupt_sidecar_is_ignored_and_replaced(tmp_path) -> None:
    out = tmp_path / "Album [WEB FLAC]"
    (tmp_path / conversions.REGISTRY_DIR).mkdir()
    (tmp_path / conversions.REGISTRY_DIR / f"{out.name}.json").write_text("{not json")

    assert conversions.conversion_of(str(out)) is None
    conversions.record_conversion(str(out), source="/x", kind="transcode", bitrate="V0")
    assert conversions.conversion_of(str(out)) == {"source": "/x", "kind": "transcode", "bitrate": "V0"}


@pytest.mark.parametrize(
    "entry",
    [
        ["not", "a", "mapping"],
        {"source": "/x", "kind": "upsample", "bit_depth": 16, "sample_rate": 44100},
        {"kind": "transcode", "bitrate": "V0"},
        {"source": "/x", "kind": "transcode", "bitrate": "V9"},
        {"source": "/x", "kind": "downconvert", "bit_depth": 32, "sample_rate": 44100},
        {"source": "/x", "kind": "downconvert", "bit_depth": 16, "sample_rate": "44100"},
        {"source": "/x", "kind": "downconvert", "bit_depth": 16, "sample_rate": []},
    ],
)
def test_an_unusable_entry_reads_as_no_conversion(tmp_path, entry) -> None:
    out = tmp_path / "Album [WEB FLAC]"
    (tmp_path / conversions.REGISTRY_DIR).mkdir()
    (tmp_path / conversions.REGISTRY_DIR / f"{out.name}.json").write_text(__import__("json").dumps(entry))

    assert conversions.conversion_of(str(out)) is None


def _stub_convert(monkeypatch, out, items):
    async def no_conversion(_items, _bit_depth):
        return None

    monkeypatch.setattr(dc, "_validate_lossless", lambda _path: None)
    monkeypatch.setattr(dc, "_build_output_path", lambda *_a: str(out))
    monkeypatch.setattr(dc, "_collect_convert_items", lambda *_a: items)
    monkeypatch.setattr(dc, "_copy_extra_files", lambda *_a, **_k: None)
    monkeypatch.setattr(dc, "_convert_audio_files", no_conversion)


def test_convert_folder_records_its_output(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB 24bit FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB FLAC]"
    _stub_convert(monkeypatch, out, [SimpleNamespace(src="01.flac", target_rate=44100)])

    rate, path = anyio.run(dc.convert_folder, str(src), 16, 44100)

    assert (rate, path) == (44100, str(out))
    assert conversions.conversion_of(str(out)) == {**DOWNCONVERT, "source": str(src)}


def test_a_mixed_family_folder_records_every_target_rate(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB 24bit FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB FLAC]"
    items = [SimpleNamespace(src="01.flac", target_rate=48000), SimpleNamespace(src="02.flac", target_rate=44100)]
    _stub_convert(monkeypatch, out, items)

    anyio.run(dc.convert_folder, str(src), 16, None)

    assert conversions.conversion_of(str(out)) == {**DOWNCONVERT, "source": str(src), "sample_rate": [44100, 48000]}


def test_nothing_converted_means_nothing_recorded(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB FLAC] (copy)"
    _stub_convert(monkeypatch, out, [])

    anyio.run(dc.convert_folder, str(src), 16, 44100)

    assert conversions.conversion_of(str(out)) is None


def _stub_transcode(monkeypatch, out, items):
    async def no_transcode(_items, _bitrate):
        return None

    monkeypatch.setattr(tc, "_validate_lossless", lambda _path: None)
    monkeypatch.setattr(tc, "_build_output_path", lambda *_a: str(out))
    monkeypatch.setattr(tc, "_collect_transcode_items", lambda *_a: items)
    monkeypatch.setattr(tc, "_validate_channel_count", lambda _items: None)
    monkeypatch.setattr(tc, "_copy_extra_files", lambda *_a, **_k: None)
    monkeypatch.setattr(tc, "_transcode_audio_files", no_transcode)


def test_transcode_folder_records_its_output(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB MP3 V0]"
    _stub_transcode(monkeypatch, out, [SimpleNamespace(src="01.flac")])

    result = anyio.run(tc.transcode_folder, str(src), "V0")

    assert result == str(out)
    assert conversions.conversion_of(str(out)) == {"source": str(src), "kind": "transcode", "bitrate": "V0"}


def test_transcode_of_nothing_records_nothing(tmp_path, monkeypatch) -> None:
    src = tmp_path / "Album [WEB FLAC]"
    src.mkdir()
    out = tmp_path / "Album [WEB MP3 V0]"
    _stub_transcode(monkeypatch, out, [])

    anyio.run(tc.transcode_folder, str(src), "V0")

    assert conversions.conversion_of(str(out)) is None


def test_conversion_description_uses_the_in_run_wording() -> None:
    url = "https://redacted.sh/torrents.php?id=2855221"
    transcode = {"source": "/x", "kind": "transcode", "bitrate": "V0"}

    assert uploader.conversion_description(DOWNCONVERT, url) == dc.generate_conversion_description(url, 44100, 16)
    assert uploader.conversion_description(transcode, url) == tc.generate_transcode_description(url, "V0")
    assert uploader.conversion_description(None, url) is None


def test_a_mixed_family_description_lists_one_sox_command_per_rate() -> None:
    description = dc.generate_conversion_description("https://redacted.sh/torrents.php?id=1", [44100, 48000], 16)

    assert "16 bit 44.1 / 48.0 kHz" in description
    assert description.count("sox input.flac") == 2
    assert "rate -v -L 44100 dither\nsox" in description and "rate -v -L 48000 dither" in description


def test_carry_conversion_follows_a_moved_folder(tmp_path) -> None:
    old, new = tmp_path / "old name", tmp_path / "new name"
    conversions.record_conversion(str(old), **DOWNCONVERT)

    conversions.carry_conversion(str(old), str(new))

    assert conversions.conversion_of(str(new)) == DOWNCONVERT
    assert conversions.conversion_of(str(old)) is None, "the old folder is gone, so its record is too"


def test_carry_conversion_keeps_the_record_while_the_old_folder_remains(tmp_path) -> None:
    old, new = tmp_path / "old name", tmp_path / "new name"
    old.mkdir()
    conversions.record_conversion(str(old), **DOWNCONVERT)

    conversions.carry_conversion(str(old), str(new))
    conversions.carry_conversion(str(old), str(old))

    assert conversions.conversion_of(str(new)) == DOWNCONVERT
    assert conversions.conversion_of(str(old)) == DOWNCONVERT


def test_a_renamed_folder_can_still_be_uploaded_with_its_note(tmp_path, monkeypatch) -> None:
    # The upload reads the record, renames the folder, and may abort; the retry must find the record again.
    monkeypatch.setattr(foldername.cfg.directory, "download_directory", str(tmp_path))
    monkeypatch.setattr(foldername.cfg.upload.formatting, "remove_source_dir", True)
    template = "{artists} - {title} ({year}) [{source} {format}]"
    monkeypatch.setattr(foldername.cfg.upload.formatting, "folder_template", template)
    album = tmp_path / "(2022) journaling"
    album.mkdir()
    (album / "01.flac").write_bytes(b"x")
    conversions.record_conversion(str(album), **DOWNCONVERT)
    metadata = {
        "artists": [("Illy", "main")],
        "title": "journaling",
        "year": 2022,
        "source": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
    }

    renamed = foldername.rename_folder(str(album), metadata, auto_rename=True, check=False)

    assert renamed == str(tmp_path / "Illy - journaling (2022) [WEB FLAC]")
    assert conversions.conversion_of(renamed) == DOWNCONVERT
    assert conversions.conversion_of(str(album)) is None
