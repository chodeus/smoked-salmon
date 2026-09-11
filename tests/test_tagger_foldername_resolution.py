"""The optional {resolution} folder token: bit depth and sample rate, only when the template asks for it."""

import pytest

from salmon import cfg
from salmon.tagger import foldername


def _audio_info(monkeypatch, precision, rate):
    calls: list[tuple[str, bool]] = []

    def fake_gather(path, sort_by_tracknumber=False):
        calls.append((path, sort_by_tracknumber))
        return {"01.flac": {"precision": precision, "sample rate": rate}}

    monkeypatch.setattr(foldername, "gather_audio_info", fake_gather)
    return calls


@pytest.mark.parametrize(
    ("precision", "rate", "expected"),
    [
        (24, 96000, "24-96"),
        (24, 44100, "24-44.1"),
        (16, 48000, "16-48"),
        (16, 44100, ""),
        (None, 44100, ""),
        (0, 44100, ""),
        (24, None, ""),
    ],
)
def test_resolution_reads_bit_depth_and_sample_rate(monkeypatch, precision, rate, expected) -> None:
    calls = _audio_info(monkeypatch, precision, rate)

    result = foldername.resolution("/music/album")

    assert result == expected
    assert calls == [("/music/album", True)], "the first track means the lowest track number, not the first filename"


WITH_TOKEN = "{artists} - {title} ({year}) [{source} FLAC {resolution}]"
METADATA = {
    "artists": [("Illy", "main")],
    "title": "journaling",
    "year": 2022,
    "source": "WEB",
    "format": "FLAC",
    "encoding": "24bit Lossless",
    "encoding_vbr": False,
    "scene": False,
}


def test_the_token_lands_in_the_folder_name_when_the_template_uses_it(monkeypatch, tmp_path) -> None:
    calls = _audio_info(monkeypatch, 24, 96000)
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", WITH_TOKEN)
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "old name"
    album.mkdir()

    renamed = foldername.rename_folder(str(album), METADATA, auto_rename=True, check=False)

    assert renamed == str(tmp_path / "Illy - journaling (2022) [WEB FLAC 24-96]")
    assert calls == [(str(album), True)]


def test_the_files_are_not_read_when_the_template_has_no_token(monkeypatch, tmp_path) -> None:
    calls = _audio_info(monkeypatch, 24, 96000)
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", "{artists} - {title} ({year}) [{source} {format}]")
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "old name"
    album.mkdir()

    renamed = foldername.rename_folder(str(album), METADATA, auto_rename=True, check=False)

    assert renamed == str(tmp_path / "Illy - journaling (2022) [WEB 24bit FLAC]")
    assert calls == []


def test_an_escaped_token_does_not_read_the_files(monkeypatch, tmp_path) -> None:
    # "{{resolution}}" is a literal "{resolution}" to str.format, not the token.
    calls = _audio_info(monkeypatch, 24, 96000)
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", "{artists} - {title} [{{resolution}}]")
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "old name"
    album.mkdir()

    foldername.rename_folder(str(album), METADATA, auto_rename=True, check=False)

    assert calls == []


def test_a_hybrid_folder_reads_the_lowest_track_number(monkeypatch) -> None:
    # "10 - ..." sorts before "2 - ..." by filename; track order must win.
    def fake_gather(path, sort_by_tracknumber=False):
        ten = {"precision": 16, "sample rate": 44100}
        two = {"precision": 24, "sample rate": 96000}
        by_name = {"10 - ten.flac": ten, "2 - two.flac": two}
        by_track = {"2 - two.flac": two, "10 - ten.flac": ten}
        return by_track if sort_by_tracknumber else by_name

    monkeypatch.setattr(foldername, "gather_audio_info", fake_gather)

    assert foldername.resolution("/music/album") == "24-96"
