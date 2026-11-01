"""Store URLs pasted at the metadata prompt: one that fails beside one that works is left out; alone, it ends the run.
And what the pre-typed answer says about an unstarred store URL."""

from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from test_tagger_metadata_source_url import make_release_data  # pyright: ignore[reportMissingImports]

from salmon.errors import ScrapeError
from salmon.sources.deezer import DeezerBase
from salmon.sources.qobuz import QobuzBase
from salmon.tagger import metadata as metadata_mod


class _FailingScraper:
    regex = QobuzBase.regex

    async def scrape_release(self, url: str):
        raise ScrapeError("no such album")


class _WorkingScraper:
    regex = DeezerBase.regex

    async def scrape_release(self, url: str):
        return {"title": "Scraped"}


def _pasted(monkeypatch, urls: str) -> None:
    async def answer(*_args, **_kwargs):
        return urls

    monkeypatch.setattr(metadata_mod.click, "prompt", answer)
    monkeypatch.setattr(
        metadata_mod,
        "METASOURCES",
        {"Qobuz": SimpleNamespace(Scraper=_FailingScraper), "Deezer": SimpleNamespace(Scraper=_WorkingScraper)},
    )


def test_a_store_url_that_fails_to_scrape_alone_ends_the_run(monkeypatch) -> None:
    _pasted(monkeypatch, "https://open.qobuz.com/album/0887396827479")

    with pytest.raises(ScrapeError, match="no such album"):
        anyio.run(metadata_mod._select_choice, {}, make_release_data())


def test_a_store_url_that_fails_beside_one_that_works_is_reported_and_left_out(monkeypatch, capsys) -> None:
    _pasted(monkeypatch, "https://open.qobuz.com/album/0887396827479 https://www.deezer.com/album/322064097")
    combined: list[tuple[list[str], list[Any]]] = []

    def combine(sources, metadatas, _rls_data, _source_url):
        combined.append((sources, metadatas))
        return {"title": "Combined"}

    monkeypatch.setattr(metadata_mod, "_combine_scraped", combine)

    metadata, _source_url = anyio.run(metadata_mod._select_choice, {}, make_release_data())

    assert metadata == {"title": "Combined"}
    assert combined == [(["Qobuz", "Deezer"], [None, {"title": "Scraped"}])]
    assert "Scrape error: no such album" in capsys.readouterr().out


FAILING, WORKING = "https://open.qobuz.com/album/0887396827479", "https://www.deezer.com/album/322064097"


def _links_given(monkeypatch) -> list[tuple[list[str], str | None]]:
    """What _combine_scraped is given: the release's links, and the source URL."""
    given: list[tuple[list[str], str | None]] = []

    def combine(_sources, _metadatas, rls_data, source_url):
        given.append((list(rls_data["urls"]), source_url))
        return {"title": "Combined"}

    monkeypatch.setattr(metadata_mod, "_combine_scraped", combine)
    return given


@pytest.mark.parametrize("star", ["", "*"])
def test_a_pasted_url_that_fails_is_left_out_of_the_links_too(monkeypatch, capsys, star: str) -> None:
    _pasted(monkeypatch, f"{star}{FAILING} {WORKING}")
    given = _links_given(monkeypatch)

    _metadata, source_url = anyio.run(metadata_mod._select_choice, {}, make_release_data())
    out = capsys.readouterr().out

    assert given == [([WORKING], None)]
    assert source_url is None
    assert f"Left out of the release's links, as it could not be read: {FAILING}" in out


def test_a_failing_url_the_tags_already_gave_stays_in_the_links(monkeypatch) -> None:
    _pasted(monkeypatch, f"*{FAILING} {WORKING}")
    given = _links_given(monkeypatch)
    rls_data = make_release_data()
    rls_data["urls"] = [FAILING]

    _metadata, source_url = anyio.run(metadata_mod._select_choice, {}, rls_data)

    assert given == [([FAILING, WORKING], FAILING)]
    assert source_url == FAILING


def test_an_unstarred_store_url_says_why_it_is_not_the_source(capsys) -> None:
    metadata_mod._explain_default("https://www.qobuz.com/album/x/0887396827479", {})

    out = capsys.readouterr().out
    assert "(not starred: not a WEB release)" in out
