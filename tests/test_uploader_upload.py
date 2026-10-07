from types import SimpleNamespace
from typing import Any

import pytest

from salmon import cfg
from salmon.uploader.upload import generate_description, generate_source_links, generate_t_description


def test_generate_source_links_excludes_source_url() -> None:
    source_url = "https://gammenterprises.bandcamp.com/album/cry-fi-dem"
    metadata_urls = [
        source_url,
        "https://www.juno.co.uk/products/riddim-research-lab-vs-lay-cry-fi-dem-vinyl/1094887-01/",
        "https://wordandsound.net/release/160089-GAMM194-Riddim-Research-Lab-vs-Lay-Far--Ant-To-Be-Cry-Fi-Dem",
    ]

    links = generate_source_links(metadata_urls, source_url)

    assert "Bandcamp" not in links
    assert f"[url={source_url}]" not in links
    assert f"[url={metadata_urls[1]}]" in links
    assert f"[url={metadata_urls[2]}]" in links


def test_generate_t_description_omits_empty_more_info_after_source_filter() -> None:
    original_icons_in_descriptions = cfg.upload.description.icons_in_descriptions
    original_include_tracklist_in_t_desc = cfg.upload.description.include_tracklist_in_t_desc

    try:
        cfg.upload.description.icons_in_descriptions = False
        cfg.upload.description.include_tracklist_in_t_desc = True

        source_url = "https://gammenterprises.bandcamp.com/album/cry-fi-dem"
        description = generate_t_description(
            metadata={"date": "2025-07-25"},
            track_data={
                "01. Cry Fi Dem (vs Lay-Far).flac": {
                    "duration": 321,
                    "bit rate": 0,
                    "precision": 24,
                    "sample rate": 44100,
                }
            },
            hybrid=False,
            metadata_urls=[source_url],
            spectral_urls=None,
            spectral_ids=None,
            lossy_comment=None,
            source_url=source_url,
        )
    finally:
        cfg.upload.description.icons_in_descriptions = original_icons_in_descriptions
        cfg.upload.description.include_tracklist_in_t_desc = original_include_tracklist_in_t_desc

    assert "[b]Source:[/b] [url=https://gammenterprises.bandcamp.com/album/cry-fi-dem]Bandcamp[/url]" in description
    assert "[b]More info:[/b]" not in description


def test_a_new_group_takes_the_description_override(monkeypatch) -> None:
    # Upstream #587: compile_data_new_group used to ignore it.
    from types import SimpleNamespace

    from salmon.uploader.upload import compile_data_new_group

    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", False)
    site = SimpleNamespace(
        site_string="RED",
        release_types={"Album": 1},
        unsupported_artist_roles=frozenset(),
        upload_form_fields=lambda metadata, track_data: {},
    )
    metadata = {
        "title": "Album",
        "artists": [("Artist", "main")],
        "group_year": 2020,
        "label": None,
        "catno": None,
        "rls_type": "Album",
        "year": 2020,
        "edition_title": None,
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "source": "WEB",
        "tags": ["electronic"],
        "comment": None,
        "urls": [],
        "date": None,
    }

    data = compile_data_new_group(
        site,  # pyright: ignore[reportArgumentType]
        path="/tmp/does-not-exist",
        metadata=metadata,
        track_data={},
        hybrid=False,
        cover_url=None,
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=None,
        override_description="[b]Source:[/b] the original",
    )

    assert data["release_desc"] == "[b]Source:[/b] the original"


def _two_track_data() -> dict[str, dict[str, Any]]:
    return {
        "01. Track One.flac": {
            "duration": 200,
            "t": SimpleNamespace(
                discnumber="1/1",
                tracknumber="1",
                artist=["Artist A", "Artist B"],
                title="Track One",
            ),
        },
        "02. Track Two.flac": {
            "duration": 180,
            "t": SimpleNamespace(
                discnumber="1/1",
                tracknumber="2",
                artist=["Artist A"],
                title="Track Two",
            ),
        },
    }


def _base_metadata() -> dict[str, Any]:
    return {"comment": None, "urls": None}


def test_generate_description_wraps_artists_in_bbcode_when_enabled() -> None:
    original = cfg.upload.description.artist_tags_in_tracklist
    try:
        cfg.upload.description.artist_tags_in_tracklist = True
        description = generate_description(_two_track_data(), _base_metadata())
    finally:
        cfg.upload.description.artist_tags_in_tracklist = original

    assert "[artist]Artist A[/artist], [artist]Artist B[/artist] - Track One" in description
    assert "[artist]Artist A[/artist] - Track Two" in description


@pytest.mark.parametrize("artist", ["A & B (feat. C)", "Artist A, Artist B", ""])
def test_a_value_naming_no_single_artist_is_left_plain(monkeypatch, artist: str) -> None:
    from salmon.uploader.upload import format_tracklist_artists

    monkeypatch.setattr(cfg.upload.description, "artist_tags_in_tracklist", True)
    formatted = format_tracklist_artists([artist])

    assert formatted == artist


def test_generate_description_leaves_artists_plain_by_default() -> None:
    original = cfg.upload.description.artist_tags_in_tracklist
    try:
        cfg.upload.description.artist_tags_in_tracklist = False
        description = generate_description(_two_track_data(), _base_metadata())
    finally:
        cfg.upload.description.artist_tags_in_tracklist = original

    assert "[artist]" not in description
    assert "Artist A, Artist B - Track One" in description
    assert "Artist A - Track Two" in description


def test_generate_description_does_not_wrap_artist_with_brackets() -> None:
    original = cfg.upload.description.artist_tags_in_tracklist
    try:
        cfg.upload.description.artist_tags_in_tracklist = True
        track_data = {
            "01. Track One.flac": {
                "duration": 200,
                "t": SimpleNamespace(
                    discnumber="1/1",
                    tracknumber="1",
                    artist=["A [B]"],
                    title="Track One",
                ),
            }
        }
        description = generate_description(track_data, _base_metadata())
    finally:
        cfg.upload.description.artist_tags_in_tracklist = original

    assert "A [B] - Track One" in description
    assert "[artist]A [B][/artist]" not in description


def test_upload_description_default_config_has_artist_tags_off() -> None:
    assert cfg.upload.description.artist_tags_in_tracklist is False
