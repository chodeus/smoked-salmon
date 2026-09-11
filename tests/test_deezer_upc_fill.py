"""A missing UPC is taken from the Deezer album the files came from, and from nowhere else."""

import anyio
import pytest

from salmon.errors import ScrapeError
from salmon.sources import deezer
from salmon.tagger import metadata as metadata_mod

DEEZER_URL = "https://www.deezer.com/en/album/322064097"
QOBUZ_URL = "https://www.qobuz.com/au-en/album/journaling-illy/ul39e7xjbuqrb"


def _deezer_answers(monkeypatch, payload):
    asked: list[str] = []

    async def fake_get_json(_self, url, params=None, headers=None):
        asked.append(url)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(deezer.DeezerBase, "get_json", fake_get_json)
    return asked


def test_album_upc_reads_the_album_by_its_id(monkeypatch) -> None:
    asked = _deezer_answers(monkeypatch, {"id": 322064097, "upc": "0656465465801"})

    upc = anyio.run(deezer.album_upc, DEEZER_URL)

    assert upc == "0656465465801"
    assert asked == ["/album/322064097"]


@pytest.mark.parametrize(
    "url",
    [
        QOBUZ_URL,
        "https://www.deezer.com/track/12345",
        "not a url",
        "https://notdeezer.com/album/322064097",
        "https://deezer.com.evil.test/album/322064097",
        "https://www.deezer.com/album/322064097junk",
    ],
)
def test_album_upc_makes_no_request_for_anything_but_a_deezer_album(monkeypatch, url: str) -> None:
    asked = _deezer_answers(monkeypatch, {"upc": "should not be read"})

    upc = anyio.run(deezer.album_upc, url)

    assert upc is None
    assert asked == []


@pytest.mark.parametrize("failure", [ScrapeError("down"), TimeoutError()])
def test_album_upc_swallows_a_failed_request(monkeypatch, failure: Exception) -> None:
    _deezer_answers(monkeypatch, failure)

    upc = anyio.run(deezer.album_upc, DEEZER_URL)

    assert upc is None


def test_the_regex_still_reads_every_real_deezer_form() -> None:
    forms = [
        "https://www.deezer.com/album/322064097",
        "https://deezer.com/album/322064097",
        "https://www.deezer.com/en/album/322064097",
        "http://www.deezer.com/fr/album/322064097",
        "https://www.deezer.com/album/322064097?utm_source=x",
        "https://www.deezer.com/album/322064097/",
        "https://www.deezer.com/album/322064097#top",
    ]

    matches = [deezer.DeezerBase.regex.search(url) for url in forms]

    assert all(matches)
    assert [match[2] for match in matches if match] == ["322064097"] * len(forms)


def test_fill_upc_from_store_fills_only_a_missing_upc(monkeypatch) -> None:
    _deezer_answers(monkeypatch, {"upc": "0656465465801"})
    missing = {"upc": None}
    present = {"upc": "1111111111111"}

    anyio.run(metadata_mod.fill_upc_from_store, missing, DEEZER_URL)
    anyio.run(metadata_mod.fill_upc_from_store, present, DEEZER_URL)

    assert missing["upc"] == "0656465465801"
    assert present["upc"] == "1111111111111"


def test_fill_upc_from_store_leaves_qobuz_sourced_files_alone(monkeypatch) -> None:
    asked = _deezer_answers(monkeypatch, {"upc": "should not be read"})
    metadata = {"upc": None}

    anyio.run(metadata_mod.fill_upc_from_store, metadata, QOBUZ_URL)
    anyio.run(metadata_mod.fill_upc_from_store, metadata, None)

    assert metadata["upc"] is None
    assert asked == []
