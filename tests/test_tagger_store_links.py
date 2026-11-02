"""A store link with a query or fragment (a share link, a tab) names the same release as its plain form."""

from types import SimpleNamespace

import anyio
import musicbrainzngs
import pytest
from test_tagger_metadata_source_url import make_release_data  # pyright: ignore[reportMissingImports]

from salmon.sources.bandcamp import BandcampBase
from salmon.sources.beatport import BeatportBase
from salmon.sources.musicbrainz import MusicBrainzBase
from salmon.sources.qobuz import QobuzBase
from salmon.sources.tidal import TidalBase
from salmon.tagger import metadata as metadata_mod
from salmon.tagger.combine import get_source_from_link


def test_a_tracked_bandcamp_link_is_the_same_album_as_its_plain_one() -> None:
    plain = metadata_mod._album_identity("https://artist.bandcamp.com/album/some-album")

    assert plain is not None
    assert metadata_mod._album_identity("https://artist.bandcamp.com/album/some-album?from=fanpub_fnb") == plain
    assert metadata_mod._album_identity("https://artist.bandcamp.com/album/some-album#buyFullDiscography") == plain


@pytest.mark.parametrize(
    ("url", "source"),
    [
        ("https://open.qobuz.com/album/0887396827479?utm_source=share", "Qobuz"),
        ("https://www.qobuz.com/gb-en/album/some-album/0887396827479#tracks", "Qobuz"),
        ("https://www.beatport.com/release/some-release/1234567?utm_source=share", "Beatport"),
        ("https://musicbrainz.org/release/0f3c8f2a-1b2c-4d5e-8f90-123456789abc?tab=tracks", "MusicBrainz"),
    ],
)
def test_a_store_link_with_a_query_is_its_own_stores(url, source) -> None:
    assert get_source_from_link(url) == source


def test_a_musicbrainz_link_with_a_query_fetches_its_release(monkeypatch) -> None:
    asked = []

    def get_release_by_id(rls_id, includes):
        asked.append(rls_id)
        return {"release": {}}

    monkeypatch.setattr(musicbrainzngs, "get_release_by_id", get_release_by_id)
    url = "https://musicbrainz.org/release/0f3c8f2a-1b2c-4d5e-8f90-123456789abc/?tab=tracks"

    anyio.run(lambda: MusicBrainzBase().fetch_data(url))

    assert asked == ["0f3c8f2a-1b2c-4d5e-8f90-123456789abc"]


@pytest.mark.parametrize(
    ("scraper", "url", "release_id"),
    [
        (QobuzBase, "https://open.qobuz.com/album/111?redirect=/album/222#tracks", "111"),
        (QobuzBase, "https://www.qobuz.com/gb-en/album/some-album/111.html?next=/album/222", None),
        (BeatportBase, "https://www.beatport.com/release/some-release/abc?next=/release/x/999", None),
    ],
)
def test_a_links_query_never_gives_its_release_id(scraper, url, release_id) -> None:
    match = scraper.regex.match(url)
    assert (match[1] if match else None) == release_id


def test_a_query_naming_another_store_leaves_the_link_its_own_stores() -> None:
    url = "https://artist.bandcamp.com/album/one?ref=https://tidal.com/album/123"

    assert get_source_from_link(url) == "Bandcamp"
    assert metadata_mod._album_identity(url) == ("Bandcamp", "artist.bandcamp.com", "one")


def test_a_tagged_album_link_whose_query_names_a_track_is_still_the_album(monkeypatch) -> None:
    url = "https://open.qobuz.com/album/123?ref=/track/456"
    monkeypatch.setattr(metadata_mod, "tag_urls", lambda _path: ([url], []))

    assert metadata_mod.files_store_url("album") == url


def test_a_tag_url_that_cannot_be_split_is_no_stores_link(monkeypatch) -> None:
    broken = "https://artist.bandcamp.com:[broken/album/foo"
    good = "https://open.qobuz.com/album/123"
    monkeypatch.setattr(metadata_mod, "tag_urls", lambda _path: ([broken, good], []))

    assert get_source_from_link(broken) is None
    assert metadata_mod.files_store_url("album") == good


def test_a_pasted_link_goes_to_its_own_stores_scraper_whatever_its_query_names(monkeypatch) -> None:
    # Tidal is tried first, and its pattern takes a tidal.com album anywhere in the link, the query too.
    url = "https://artist.bandcamp.com/album/one?ref=https://tidal.com/album/123"
    scraped: list[str] = []

    def source(name: str, pattern):
        class Scraper:
            regex = pattern

            async def scrape_release(self, _url: str):
                scraped.append(name)
                return {"title": name}

        return SimpleNamespace(Scraper=Scraper)

    async def answer(*_args, **_kwargs):
        return url

    monkeypatch.setattr(metadata_mod.click, "prompt", answer)
    monkeypatch.setattr(
        metadata_mod,
        "METASOURCES",
        {"Tidal": source("Tidal", TidalBase.regex), "Bandcamp": source("Bandcamp", BandcampBase.regex)},
    )
    monkeypatch.setattr(metadata_mod, "_combine_scraped", lambda *_args: {"title": "Combined"})

    anyio.run(metadata_mod._select_choice, {}, make_release_data())

    assert scraped == ["Bandcamp"]
