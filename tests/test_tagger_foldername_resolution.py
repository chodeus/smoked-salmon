"""The optional {resolution} folder token: bit depth and sample rate, only when the template asks for it."""

import pytest

from salmon import cfg
from salmon.tagger import foldername


def _audio_info(monkeypatch, precision, rate):
    calls: list[str] = []

    def fake_gather(path, sort_by_tracknumber=False):
        calls.append(path)
        return {"01.flac": {"precision": precision, "sample rate": rate}}

    monkeypatch.setattr(foldername, "gather_audio_info", fake_gather)
    return calls


@pytest.mark.parametrize(
    ("precision", "rate", "expected"),
    [(24, 96000, "24-96"), (24, 44100, "24-44.1"), (16, 48000, "16-48"), (16, 44100, ""), (None, 44100, "")],
)
def test_resolution_reads_bit_depth_and_sample_rate(monkeypatch, precision, rate, expected) -> None:
    _audio_info(monkeypatch, precision, rate)

    assert foldername.resolution("/music/album") == expected


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
    assert calls == [str(album)]


def test_the_files_are_not_read_when_the_template_has_no_token(monkeypatch, tmp_path) -> None:
    calls = _audio_info(monkeypatch, 24, 96000)
    monkeypatch.setattr(cfg.upload.formatting, "folder_template", "{artists} - {title} ({year}) [{source} {format}]")
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    album = tmp_path / "old name"
    album.mkdir()

    renamed = foldername.rename_folder(str(album), METADATA, auto_rename=True, check=False)

    assert renamed == str(tmp_path / "Illy - journaling (2022) [WEB 24bit FLAC]")
    assert calls == []
