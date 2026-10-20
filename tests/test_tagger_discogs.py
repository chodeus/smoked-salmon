"""The artists a Discogs credit list names: a feat. join makes guests, and (N) and * are dropped."""

import asyncio
import json

from salmon.tagger.sources.base import append_remixers_to_track_titles, generate_artists
from salmon.tagger.sources.discogs import Scraper, parse_artists, parse_credits


def make_soup(tracklist: list[dict]) -> dict:
    return {"tracklist": tracklist, "artists": [{"name": "Various"}]}


def artist(name: str, join: str = "") -> dict:
    return {"name": name, "join": join}


def test_discogs_feat_join_makes_following_artists_guests() -> None:
    # Discogs release 2520452, track 4-11
    track = {"artists": [artist("Shawfire", "Feat."), artist("Ultra (27)", "&"), artist("Tullisa")]}

    assert parse_artists([{"name": "Various"}], track) == [
        ("Shawfire", "main"),
        ("Ultra", "guest"),
        ("Tullisa", "guest"),
    ]


def test_discogs_feat_join_spellings() -> None:
    for join in ("Featuring", "featuring", "Ft", "ft.", "FEAT", "Feat."):
        track = {"artists": [artist("Lead", join), artist("Guest")]}

        assert parse_artists([], track) == [("Lead", "main"), ("Guest", "guest")], join


def test_discogs_feat_join_applies_to_release_artists_without_track_artists() -> None:
    release_artists = [artist("Lead", "Feat."), artist("Guest")]

    assert parse_artists(release_artists, {}) == [("Lead", "main"), ("Guest", "guest")]


def test_discogs_other_joins_keep_artists_main() -> None:
    for join in ("&", ",", "Vs.", "And", "With", ""):
        track = {"artists": [artist("One", join), artist("Two")]}

        assert parse_artists([], track) == [("One", "main"), ("Two", "main")], join


def test_discogs_feat_join_is_not_matched_inside_a_longer_word() -> None:
    track = {"artists": [artist("One", "Featured"), artist("Two")]}

    assert parse_artists([], track) == [("One", "main"), ("Two", "main")]


def test_discogs_guest_after_feat_join_is_not_added_again_from_extraartists() -> None:
    track = {
        "artists": [artist("Lead", "Feat."), artist("Guest")],
        "extraartists": [
            {"name": "Guest", "role": "Featuring"},
            {"name": "Guest", "role": "Vocals, Featuring"},
        ],
    }

    assert parse_artists([], track) == [("Lead", "main"), ("Guest", "guest")]


def test_discogs_extraartist_with_another_role_is_kept_for_the_same_name() -> None:
    track = {
        "artists": [artist("Lead", "Feat."), artist("Guest")],
        "extraartists": [{"name": "Guest", "role": "Producer"}],
    }

    assert parse_artists([], track) == [("Lead", "main"), ("Guest", "guest"), ("Guest", "producer")]


def test_discogs_asterisk_is_stripped_before_the_numbered_suffix() -> None:
    track = {"artists": [artist("Lead", "Feat."), artist("Guest (27)*")]}

    assert parse_artists([], track) == [("Lead", "main"), ("Guest", "guest")]


def test_discogs_vs_join_keeps_both_artists_main_and_adds_remixers() -> None:
    # Discogs release 2520452, track 1-16
    track = {
        "artists": [artist("Selena (5)", "Vs."), {"name": "X.Men", "anv": "X-Men", "join": ""}],
        "extraartists": [
            {"name": "DJ Face", "anv": "Face", "join": "", "role": "Remix"},
            {"name": 'Matt "Jam" Lamont', "anv": "Jamm", "join": "", "role": "Remix"},
        ],
    }

    assert parse_artists([{"name": 'Matt "Jam" Lamont'}], track) == [
        ("Selena", "main"),
        ("X.Men", "main"),
        ("DJ Face", "remixer"),
        ('Matt "Jam" Lamont', "remixer"),
    ]


def test_discogs_reads_the_anv_of_track_artists_and_extraartists() -> None:
    # Discogs release 2520452, track 4-6 (the Remix credit has no ANV there; "Trouble Men" is given one here)
    track = {
        "artists": [
            {"name": "Gomi", "anv": "DJ Gomi", "join": "Feat."},
            {"name": 'Louie "Balo" Guzman', "anv": "Loui Balo", "join": "&"},
            {"name": "Yasmeen", "anv": "", "join": ""},
        ],
        "extraartists": [{"name": "Trouble Men", "anv": "The Trouble Men", "join": "", "role": "Remix"}],
    }

    artists, variations = parse_credits([], track)

    assert artists == [
        ("Gomi", "main"),
        ('Louie "Balo" Guzman', "guest"),
        ("Yasmeen", "guest"),
        ("Trouble Men", "remixer"),
    ]
    assert variations == [
        {"name": "Gomi", "role": "main", "anv": "DJ Gomi"},
        {"name": 'Louie "Balo" Guzman', "role": "guest", "anv": "Loui Balo"},
        {"name": "Trouble Men", "role": "remixer", "anv": "The Trouble Men"},
    ]


def test_discogs_anv_equal_to_the_name_or_empty_is_not_a_variation() -> None:
    track = {"artists": [{"name": "Ultra (27)", "anv": "Ultra"}, {"name": "Tullisa", "anv": ""}, {"name": "Lead"}]}

    assert parse_credits([], track)[1] == []


def test_discogs_scraper_carries_the_anv_beside_the_track_artists() -> None:
    tracklist = [
        {
            "type_": "track",
            "position": "3-3",
            "title": "Tell Me It's Real",
            "artists": [{"name": "K-Ci & JoJo", "anv": "K-Ci And JoJo", "join": ""}],
        }
    ]

    tracks = asyncio.run(Scraper().parse_tracks(make_soup(tracklist)))

    assert tracks["3"]["3"]["artists"] == [("K-Ci & JoJo", "main")]
    assert tracks["3"]["3"]["artist_variations"] == [{"name": "K-Ci & JoJo", "role": "main", "anv": "K-Ci And JoJo"}]
    # Plain data: a scrape that is shown as JSON must not hold tuple keys.
    json.dumps(tracks)


def test_discogs_remix_credit_does_not_add_a_second_version_to_the_title() -> None:
    # The title already names its version; the remixer is only credited.
    tracklist = [
        {
            "type_": "track",
            "position": "1",
            "title": "Nobody Else (London Connection Edit)",
            "artists": [{"name": "Lead", "anv": "", "join": ""}],
            "extraartists": [{"name": "London Connection", "anv": "", "join": "", "role": "Remix"}],
        }
    ]

    tracks = asyncio.run(Scraper().parse_tracks(make_soup(tracklist)))
    _, tracks = generate_artists(tracks)
    tracks = append_remixers_to_track_titles(tracks)

    assert tracks["1"]["1"]["title"] == "Nobody Else (London Connection Edit)"
    assert ("London Connection", "remixer") in tracks["1"]["1"]["artists"]
