"""`salmon descgen` closes every BBCode tag it opens. No metadata source is contacted."""

import re

import anyio
import pytest
from asyncclick.testing import CliRunner

import salmon.uploader.description as description_mod
from salmon import cfg
from salmon.commands import descgen
from salmon.tagger.sources import METASOURCES

METADATA = {
    "tracks": {
        "1": {
            "1": {"artists": [("Artist", "main")], "title": "First"},
            "2": {"artists": [("Artist", "main")], "title": "Second"},
        }
    },
    "comment": None,
    "urls": [],
}


def test_descgen_closes_the_tracklist_heading_size(monkeypatch, capsys) -> None:
    async def fake_run_metadata(url, return_source_name=False):
        return METADATA, "Qobuz"

    # The fork builds the description in uploader/description.py, shared with the web interface.
    monkeypatch.setattr(description_mod, "run_metadata", fake_run_metadata)
    monkeypatch.setattr(description_mod, "combine_metadatas", lambda *_: METADATA)
    monkeypatch.setattr(description_mod, "clean_metadata", lambda metadata: metadata)
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", False)
    assert descgen.callback is not None

    anyio.run(descgen.callback, ("https://www.qobuz.com/album/x",))

    out = capsys.readouterr().out
    assert "[b][size=4]Tracklist[/size][/b]" in out
    assert len(re.findall(r"\[size=\d+\]", out)) == len(re.findall(r"\[/size\]", out))


def _run_descgen(monkeypatch, *args: str):
    async def no_scrape(url, return_source_name=False):
        raise AssertionError(f"descgen scraped {url}")

    monkeypatch.setattr(description_mod, "run_metadata", no_scrape)

    async def run():
        return await CliRunner().invoke(descgen, list(args))

    return anyio.run(run)


def test_descgen_given_a_folder_says_it_takes_release_urls(monkeypatch, tmp_path) -> None:
    album = tmp_path / "Artist - Album (2024) [FLAC]"
    album.mkdir()

    result = _run_descgen(monkeypatch, str(album))

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert f"{album} is a folder; descgen takes release URLs from " in lines[0]
    assert "`salmon up` writes the description itself" in lines[0]
    assert all(name in lines[0] for name in METASOURCES)


def test_descgen_given_an_unsupported_url_lists_the_sources(monkeypatch) -> None:
    url = "https://shop.example.com/releases/123"

    result = _run_descgen(monkeypatch, url, "https://www.qobuz.com/us-en/album/x/abc123")

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "Argument 1 (shop.example.com) is not a release URL descgen supports" in lines[0]
    assert all(name in lines[0] for name in METASOURCES)


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("https://tracker.example/test-passkey-0001/announce", "test-passkey-0001"),
        ("https://tracker.example/torrents.php?action=download&id=1&authkey=test-authkey-0001", "test-authkey-0001"),
    ],
    ids=["passkey-in-path", "authkey-in-query"],
)
def test_descgen_names_only_the_host_of_an_unsupported_url(monkeypatch, url: str, secret: str) -> None:
    result = _run_descgen(monkeypatch, url)

    assert result.exit_code == 1
    assert "Argument 1 (tracker.example) is not a release URL descgen supports" in result.output
    assert secret not in result.output
