"""Tidal's v2 API (issue #419), against a local fake server; never contacts Tidal."""

import asyncio
import copy
import time
from collections.abc import Callable
from email.utils import formatdate
from types import SimpleNamespace
from typing import Any

import aiohttp
import anyio
import pytest
from aiohttp import web

from salmon import cfg
from salmon.common.urls import parse_retry_after
from salmon.errors import ScrapeError
from salmon.search.tidal import COUNTRIES, Searcher
from salmon.sources import base as sources_base
from salmon.sources import tidal as tidal_source
from salmon.sources.tidal import MAX_PAGES, TidalBase, credentials_configured
from salmon.tagger.sources.tidal import Scraper

ALBUM_URL = "https://tidal.com/album/75194842"


def _track(
    track_id: str, title: str, artist_ids: list[str], isrc: str, version: str | None = None, explicit: bool = False
) -> dict:
    return {
        "id": track_id,
        "type": "tracks",
        "attributes": {
            "title": title,
            "version": version,
            "isrc": isrc,
            "explicit": explicit,
            "mediaTags": ["LOSSLESS", "HIRES_LOSSLESS"],
        },
        "relationships": {"artists": {"data": [{"id": a, "type": "artists"} for a in artist_ids]}},
    }


def _artist(artist_id: str, name: str) -> dict:
    return {"id": artist_id, "type": "artists", "attributes": {"name": name}}


def _item(track_id: str, disc: int, number: int) -> dict:
    return {"id": track_id, "type": "tracks", "meta": {"volumeNumber": disc, "trackNumber": number}}


# A trimmed /albums/{id}?include=artists,items,items.artists,coverArt document: two tracks on
# the first page of items, and a cursor to a second page.
ALBUM_DOC = {
    "data": {
        "id": "75194842",
        "type": "albums",
        "attributes": {
            "title": "Accept & Connect",
            "albumType": "EP",
            "barcodeId": "0617465881222",
            "releaseDate": "2018-03-02",
            "explicit": False,
            "numberOfItems": 3,
            "copyright": {"text": "2018 Majestic Casual Records"},
            "mediaTags": ["LOSSLESS"],
        },
        "relationships": {
            "artists": {"data": [{"id": "a1", "type": "artists"}]},
            "coverArt": {"data": [{"id": "art1", "type": "artworks"}]},
            "items": {
                "data": [_item("t1", 1, 1), _item("t2", 1, 2)],
                "links": {"self": "/albums/75194842/relationships/items", "meta": {"nextCursor": "page2"}},
            },
        },
    },
    # Artists out of credit order, so a track's artists must follow its relationship.
    "included": [
        _artist("a2", "Guest Singer"),
        _artist("a3", "Tiësto"),
        _artist("a1", "Kordz"),
        _track("t1", "Accept", ["a1", "a2"], "USUM71800001"),
        _track("t2", "Connect", ["a1", "a3"], "USUM71800002", version="Tiësto Remix", explicit=True),
        {
            "id": "art1",
            "type": "artworks",
            "attributes": {
                "mediaType": "IMAGE",
                # The largest file is neither first nor last.
                "files": [
                    {"href": "https://resources.tidal.com/images/a/b/640x640.jpg", "meta": {"width": 640}},
                    {"href": "https://resources.tidal.com/images/a/b/1280x1280.jpg", "meta": {"width": 1280}},
                    {"href": "https://resources.tidal.com/images/a/b/80x80.jpg", "meta": {"width": 80}},
                ],
            },
        },
    ],
}

# The second page of items, whose track has an artist not seen on the first page.
ITEMS_PAGE_2 = {
    "data": [_item("t3", 2, 1)],
    "included": [_artist("a4", "Second Page Artist"), _track("t3", "Bonus", ["a1", "a4"], "USUM71800003")],
    "links": {"self": "/albums/75194842/relationships/items?page[cursor]=page2"},
}

SEARCH_DOC = {
    "data": [
        {
            "id": "kordz",
            "type": "searchResults",
            "relationships": {
                "albums": {"data": [{"id": "75194842", "type": "albums"}]},
                "tracks": {"data": []},
            },
        }
    ],
    "included": [
        _artist("a1", "Kordz"),
        {
            "id": "75194842",
            "type": "albums",
            "attributes": ALBUM_DOC["data"]["attributes"],
            "relationships": {"artists": {"data": [{"id": "a1", "type": "artists"}]}},
        },
    ],
}


class FakeTidal:
    """Record requests to a fake Tidal auth and API server."""

    def __init__(self) -> None:
        self.token_requests: list[dict] = []
        self.api_requests: list[web.Request] = []
        self.routes: dict[str, Callable[[web.Request], web.Response]] = {}
        self.runner: web.AppRunner | None = None

    async def _token(self, request: web.Request) -> web.Response:
        self.token_requests.append(dict(await request.post()))
        # Each token is distinct, so a retry that reuses a rejected one shows in its header.
        token = "fake-token" if len(self.token_requests) == 1 else f"fake-token-{len(self.token_requests)}"
        # Short enough that reading it as milliseconds would expire it at once.
        return web.json_response({"access_token": token, "token_type": "Bearer", "expires_in": 3600})

    async def _api(self, request: web.Request) -> web.Response:
        self.api_requests.append(request)
        handler = self.routes.get(request.match_info["path"])
        if handler is None:
            return web.json_response({"errors": [{"status": "404"}]}, status=404)
        return handler(request)

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/oauth2/token", self._token)
        app.router.add_get("/v2/{path:.*}", self._api)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        return f"http://127.0.0.1:{self.runner.addresses[0][1]}"

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


@pytest.fixture
def tidal(monkeypatch: pytest.MonkeyPatch) -> FakeTidal:
    """Point every Tidal client at a fake server, with client credentials and a cold token cache."""
    monkeypatch.setattr(cfg.metadata.tidal, "client_id", "an-id")
    monkeypatch.setattr(cfg.metadata.tidal, "client_secret", "a-secret")
    monkeypatch.setattr(TidalBase, "_access_token", None)
    monkeypatch.setattr(TidalBase, "_token_expiry", 0.0)
    # The fake server is on loopback, which the public-only connector refuses.
    monkeypatch.setattr(sources_base, "_public_only_session", lambda timeout: aiohttp.ClientSession(timeout=timeout))
    return FakeTidal()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the waits a rate-limited request asks for instead of sleeping them."""
    waits: list[float] = []
    real_sleep = anyio.sleep

    async def record(delay: float) -> None:
        waits.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(tidal_source.anyio, "sleep", record)
    return waits


def _run(fake: FakeTidal, monkeypatch: pytest.MonkeyPatch, body: Callable[[], Any]) -> Any:
    async def main() -> Any:
        base = await fake.start()
        monkeypatch.setattr(TidalBase, "url", f"{base}/v2")
        monkeypatch.setattr(TidalBase, "token_url", f"{base}/v1/oauth2/token")
        try:
            return await body()
        finally:
            await fake.stop()

    return anyio.run(main)


def _json(doc: dict, status: int = 200, headers: dict | None = None) -> Callable[[web.Request], web.Response]:
    return lambda request: web.json_response(doc, status=status, headers=headers)


def test_one_client_credentials_token_serves_concurrent_and_later_requests(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch
) -> None:
    tidal.routes["ping"] = _json({"data": []})

    async def body() -> None:
        # A cold cache hit by a search over several regions at once, then more requests.
        await asyncio.gather(*(Searcher().get_json("/ping") for _ in range(5)))
        await Scraper().get_json("/ping")

    _run(tidal, monkeypatch, body)
    assert tidal.token_requests == [
        {"grant_type": "client_credentials", "client_id": "an-id", "client_secret": "a-secret"}
    ]
    assert len(tidal.api_requests) == 6
    assert {r.headers["Authorization"] for r in tidal.api_requests} == {"Bearer fake-token"}
    assert {r.headers["Accept"] for r in tidal.api_requests} == {"application/vnd.api+json"}


def test_token_is_refreshed_a_minute_before_it_expires(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["ping"] = _json({"data": []})

    async def body() -> None:
        await Scraper().get_json("/ping")
        TidalBase._token_expiry = time.monotonic() + 30
        await Scraper().get_json("/ping")

    _run(tidal, monkeypatch, body)
    assert len(tidal.token_requests) == 2
    assert [r.headers["Authorization"] for r in tidal.api_requests] == ["Bearer fake-token", "Bearer fake-token-2"]


def test_album_is_parsed_into_salmon_metadata_across_item_pages(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch
) -> None:
    tidal.routes["albums/75194842"] = _json(ALBUM_DOC)
    tidal.routes["albums/75194842/relationships/items"] = _json(ITEMS_PAGE_2)

    data = _run(tidal, monkeypatch, lambda: Scraper().scrape_release(ALBUM_URL, rls_id=("JP", "75194842")))

    album_request, page_request = tidal.api_requests
    assert album_request.query["countryCode"] == page_request.query["countryCode"] == "JP"
    assert album_request.query["include"] == "artists,items,items.artists,coverArt"
    assert page_request.query["page[cursor]"] == "page2"
    assert "items.artists" in page_request.query["include"].split(",")

    assert data["title"] == "Accept & Connect"
    assert data["date"] == "2018-03-02"
    assert data["year"] == 2018
    assert data["label"] == "Majestic Casual Records"
    assert data["upc"] == "0617465881222"
    assert data["cover"] == "https://resources.tidal.com/images/a/b/1280x1280.jpg"
    assert ("Kordz", "main") in data["artists"]

    tracks = data["tracks"]
    assert sorted(tracks) == ["1", "2"]
    accept, connect, bonus = tracks["1"]["1"], tracks["1"]["2"], tracks["2"]["1"]
    assert (accept["title"], accept["isrc"], accept["explicit"]) == ("Accept", "USUM71800001", False)
    assert accept["format"] == "HI_RES"
    assert accept["artists"] == [("Kordz", "main"), ("Guest Singer", "guest")]
    assert ("Tiësto", "remixer") in connect["artists"]
    assert connect["explicit"] is True
    # The second page's track keeps its artists.
    assert bonus["artists"] == [("Kordz", "main"), ("Second Page Artist", "guest")]


def test_item_paging_stops_at_its_cap(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["albums/75194842"] = _json(ALBUM_DOC)
    # A cursor that never runs out.
    tidal.routes["albums/75194842/relationships/items"] = _json(
        {"data": [], "links": {"self": "x", "meta": {"nextCursor": "again"}}}
    )

    with pytest.raises(ScrapeError):
        _run(tidal, monkeypatch, lambda: Scraper().fetch_data(ALBUM_URL))
    assert len(tidal.api_requests) == 1 + MAX_PAGES


def test_rate_limited_request_waits_for_retry_after(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    # Not the first backoff step (1 s), so ignoring the header shows.
    responses = iter([_json({}, 429, {"Retry-After": "5"}), _json({"data": []})])
    tidal.routes["ping"] = lambda request: next(responses)(request)

    result = _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert result == {"data": []}
    assert len(tidal.api_requests) == 2
    assert sleeps == [5.0]


def test_rate_limit_retries_are_bounded(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["ping"] = _json({}, 429, {"Retry-After": "0"})

    with pytest.raises(ScrapeError):
        _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert len(tidal.api_requests) == 1 + tidal_source.RATE_LIMIT_RETRIES


def test_rate_limit_asking_for_a_long_wait_is_not_retried(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    tidal.routes["ping"] = _json({}, 429, {"Retry-After": "3600"})

    with pytest.raises(ScrapeError):
        _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert len(tidal.api_requests) == 1
    assert sleeps == []


def test_rate_limit_wait_of_exactly_the_cap_is_retried(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    cap = f"{tidal_source.MAX_RETRY_WAIT:g}"
    responses = iter([_json({}, 429, {"Retry-After": cap}), _json({"data": []})])
    tidal.routes["ping"] = lambda request: next(responses)(request)

    result = _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert result == {"data": []}
    assert sleeps == [tidal_source.MAX_RETRY_WAIT]


def test_rate_limit_with_a_non_numeric_wait_backs_off(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    rate_limited = _json({}, 429, {"Retry-After": "nan"})
    responses = iter([rate_limited, rate_limited, _json({"data": []})])
    tidal.routes["ping"] = lambda request: next(responses)(request)

    result = _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert result == {"data": []}
    assert len(tidal.api_requests) == 3
    # Exponential steps from 1 s.
    assert sleeps == [1.0, 2.0]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
def test_retry_after_dates_count_from_utc_and_a_past_date_means_no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    # A "-0000" date parses naive; read as local time it would be off by the zone's offset.
    # A POSIX TZ string needs no zoneinfo files, so the offset is really in effect.
    monkeypatch.setenv("TZ", "AWST-8")
    time.tzset()
    try:
        offset = time.timezone
        soon = parse_retry_after(formatdate(time.time() + 10))
        past = parse_retry_after(formatdate(time.time() - 10))
    finally:
        monkeypatch.undo()
        time.tzset()
    assert offset == -8 * 3600
    assert soon is not None and 5 < soon <= 10
    assert past is None
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("-5") == 0.0


def test_rejected_token_is_replaced_once(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter([_json({}, 401), _json({"data": []})])
    tidal.routes["ping"] = lambda request: next(responses)(request)

    result = _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert result == {"data": []}
    assert len(tidal.token_requests) == 2
    assert len(tidal.api_requests) == 2
    assert [r.headers["Authorization"] for r in tidal.api_requests] == ["Bearer fake-token", "Bearer fake-token-2"]


def test_rejected_token_another_request_already_replaced_is_kept(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_after_a_replacement(request: web.Request) -> web.Response:
        TidalBase._access_token = "replaced-elsewhere"
        return web.json_response({}, status=401)

    responses = iter([reject_after_a_replacement, _json({"data": []})])
    tidal.routes["ping"] = lambda request: next(responses)(request)

    result = _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert result == {"data": []}
    assert len(tidal.token_requests) == 1
    assert [r.headers["Authorization"] for r in tidal.api_requests] == [
        "Bearer fake-token",
        "Bearer replaced-elsewhere",
    ]


def test_token_rejected_twice_is_an_error(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["ping"] = _json({}, 401)

    with pytest.raises(ScrapeError):
        _run(tidal, monkeypatch, lambda: Scraper().get_json("/ping"))
    assert len(tidal.token_requests) == 2
    assert len(tidal.api_requests) == 2


def test_artist_release_paging_fails_at_its_cap(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["artists/a1/relationships/albums"] = _json(
        {"data": [], "links": {"self": "x", "meta": {"nextCursor": "again"}}}
    )

    with pytest.raises(ScrapeError):
        _run(tidal, monkeypatch, lambda: Searcher()._get_artist_albums("a1", "US"))
    assert [r.query.get("page[cursor]") for r in tidal.api_requests] == [None] + ["again"] * (MAX_PAGES - 1)


def test_artist_releases_are_collected_across_pages(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    first = SEARCH_DOC["included"][1]
    second = {
        "id": "2",
        "type": "albums",
        "attributes": {**first["attributes"], "title": "Second", "releaseDate": "2020-05-01"},
        "relationships": {"artists": {"data": [{"id": "a5", "type": "artists"}]}},
    }
    pages = {
        None: {
            "data": [{"id": first["id"], "type": "albums"}],
            "included": [_artist("a1", "Kordz"), first],
            "links": {"self": "x", "meta": {"nextCursor": "p2"}},
        },
        "p2": {
            "data": [{"id": "2", "type": "albums"}],
            "included": [_artist("a5", "Other"), second],
            "links": {"self": "x"},
        },
    }
    tidal.routes["artists/a1/relationships/albums"] = lambda request: web.json_response(
        pages[request.query.get("page[cursor]")]
    )

    releases = _run(tidal, monkeypatch, lambda: Searcher()._get_artist_albums("a1", "US"))
    assert [(r.album, r.artist, r.year) for r in releases] == [
        ("Accept & Connect", "Kordz", 2018),
        ("Second", "Other", 2020),
    ]


def test_a_failed_artist_release_page_is_an_error_not_no_releases(
    tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch
) -> None:
    tidal.routes["searchResults"] = _json({"data": [], "included": [_artist("a1", "Kordz")]})
    tidal.routes["artists/a1/relationships/albums"] = _json({}, 500)

    with pytest.raises(ScrapeError):
        _run(tidal, monkeypatch, lambda: Searcher().get_artist_releases("Kordz"))
    assert any(r.match_info["path"] == "artists/a1/relationships/albums" for r in tidal.api_requests)


def test_a_hi_res_duplicate_of_a_lossless_release_is_dropped() -> None:
    releases = [
        SimpleNamespace(url="a", quality="LOSSLESS", album="Same", year=2020),
        SimpleNamespace(url="b", quality="HI_RES", album="Same", year=2020),
        SimpleNamespace(url="c", quality="HI_RES", album="Other", year=2020),
        SimpleNamespace(url="a", quality="LOSSLESS", album="Same", year=2020),
    ]
    assert [r.url for r in Searcher._filter_dupes(releases)] == ["a", "c"]


@pytest.mark.parametrize(
    ("links", "cursor"),
    [
        ({"self": "x", "meta": {"nextCursor": "abc"}}, "abc"),
        ({"self": "x", "next": "/albums/1/relationships/items?countryCode=US&page[cursor]=abc%3D"}, "abc="),
        ({"self": "x"}, None),
    ],
    ids=["meta cursor", "next link only", "last page"],
)
def test_next_cursor_reads_meta_or_the_next_link(links: dict, cursor: str | None) -> None:
    assert TidalBase.next_cursor(links) == cursor


def test_undated_artist_releases_sort_after_dated_ones() -> None:
    releases = [
        SimpleNamespace(url=url, quality="LOSSLESS", album=url, year=year)
        for url, year in [("a", None), ("b", 2018), ("c", 2021)]
    ]
    ordered = Searcher._filter_dupes(releases)
    assert [r.year for r in ordered] == [2021, 2018, None]


def test_search_uses_the_search_results_collection(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    tidal.routes["searchResults"] = _json(SEARCH_DOC)

    source, releases = _run(tidal, monkeypatch, lambda: Searcher().search_releases("kordz accept", 5))

    assert source == "Tidal"
    assert sorted(r.query["countryCode"] for r in tidal.api_requests) == sorted(COUNTRIES)
    assert all(r.query["filter[query]"] == "kordz accept" for r in tidal.api_requests)
    assert {"albums.artists", "tracks.albums"} <= set(tidal.api_requests[0].query["include"].split(","))
    # One release, found in every configured region and deduplicated.
    ((cc, rls_id),) = releases
    assert rls_id == "75194842"
    ident = releases[(cc, rls_id)][0]
    assert (ident.artist, ident.album, ident.year, ident.track_count) == ("Kordz", "Accept & Connect", 2018, 3)


def test_search_finds_a_single_through_its_matching_track(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = copy.deepcopy(SEARCH_DOC)
    doc["data"][0]["relationships"] = {"albums": {"data": []}, "tracks": {"data": [{"id": "t1", "type": "tracks"}]}}
    track = _track("t1", "Accept", ["a1"], "USUM71800001")
    track["relationships"]["albums"] = {"data": [{"id": "75194842", "type": "albums"}]}
    doc["included"].append(track)
    tidal.routes["searchResults"] = _json(doc)

    _, releases = _run(tidal, monkeypatch, lambda: Searcher().search_releases("kordz accept", 5))
    assert [rls_id for _, rls_id in releases] == ["75194842"]


def test_an_undated_unlabelled_search_result_shows_no_none(tidal: FakeTidal, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = copy.deepcopy(SEARCH_DOC)
    attributes = doc["included"][1]["attributes"]
    del attributes["releaseDate"], attributes["copyright"]
    tidal.routes["searchResults"] = _json(doc)

    _, releases = _run(tidal, monkeypatch, lambda: Searcher().search_releases("kordz accept", 5))

    ((ident, shown),) = releases.values()
    assert ident.year is None
    assert "None" not in str(shown)


@pytest.mark.parametrize(
    ("token", "notified"),
    [("a-token-from-the-web-player", True), ("your-token", False), (None, False)],
)
def test_retired_token_alone_leaves_tidal_off_with_one_notice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], token: str | None, notified: bool
) -> None:
    monkeypatch.setattr(cfg.metadata.tidal, "client_id", None)
    monkeypatch.setattr(cfg.metadata.tidal, "client_secret", None)
    monkeypatch.setattr(cfg.metadata.tidal, "token", token)
    tidal_source._notify_retired_token.cache_clear()

    # Asked twice: the notice still prints only once.
    configured = [credentials_configured(), credentials_configured()]
    assert configured == [False, False]

    out = capsys.readouterr().out
    assert out.count("developer.tidal.com") == (1 if notified else 0)


@pytest.mark.parametrize("tags", [["LOSSLESS", "HIRES_LOSSLESS"], ["HIRES_LOSSLESS", "LOSSLESS"]])
def test_quality_is_the_best_tag_whatever_the_order(tags: list[str]) -> None:
    assert tidal_source.parse_quality(tags) == "HI_RES"
