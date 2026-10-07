"""The Arranger artist role: credited on RED and OPS, dropped on DIC."""

from typing import TYPE_CHECKING, cast

from bs4 import BeautifulSoup

from salmon import cfg
from salmon.tagger import foldername, metadata_validator_base
from salmon.tagger.sources.discogs import parse_artists
from salmon.trackers.dic import DICApi
from salmon.trackers.red import _parse_upload_form
from salmon.uploader.upload import compile_data_new_group

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


def test_discogs_arranged_by_maps_to_arranger() -> None:
    artist_soup = [{"name": "Main Artist"}]
    track = {
        "artists": [{"name": "Main Artist"}],
        "extraartists": [{"name": "Some Arranger", "role": "Arranged By"}],
    }

    artists = parse_artists(artist_soup, track)

    assert ("Main Artist", "main") in artists
    assert ("Some Arranger", "arranger") in artists


def test_discogs_arranged_by_keeps_main_for_the_same_artist() -> None:
    track = {
        "artists": [{"name": "Main Artist"}],
        "extraartists": [{"name": "Main Artist", "role": "Arranged By"}],
    }

    artists = parse_artists([], track)

    assert ("Main Artist", "main") in artists
    assert ("Main Artist", "arranger") in artists


def test_compile_artist_str_excludes_arranger() -> None:
    # Only main artists appear in the folder name, exactly like producer already does.
    artist_str = foldername._compile_artist_str(
        [("Main Artist", "main"), ("Some Arranger", "arranger"), ("Some Producer", "producer")]
    )

    assert artist_str == "Main Artist"


def _valid_metadata(**overrides):
    metadata = {
        "artists": [("Main Artist", "main"), ("Some Arranger", "arranger")],
        "tracks": {"1": {"1": {"artists": [("Main Artist", "main")]}}},
        "year": 2020,
        "rls_type": "Album",
        "genres": ["Electronic"],
        "source": "WEB",
        "label": "Test Label",
        "catno": None,
    }
    metadata.update(overrides)
    return metadata


def test_metadata_validator_accepts_arranger() -> None:
    metadata = _valid_metadata()

    result = metadata_validator_base(metadata)

    assert result is metadata
    assert ("Some Arranger", "arranger") in metadata["artists"]


def test_parse_upload_form_round_trips_arranger_importance() -> None:
    html = """
    <form>
        <input name="artists[]" value="Main Artist">
        <select name="importance[]"><option value="1" selected>Main</option></select>
        <input name="artists[]" value="Some Arranger">
        <select name="importance[]"><option value="8" selected>Arranger</option></select>
    </form>
    """
    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}

    _parse_upload_form(data, soup)

    assert data["artists[]"] == ["Main Artist", "Some Arranger"]
    assert data["importance[]"] == [1, 8]


class _FakeGazelleSite:
    def __init__(self, site_string: str, unsupported_artist_roles: frozenset[str] = frozenset()) -> None:
        self.site_string = site_string
        self.release_types = {"Album": 1}
        self.unsupported_artist_roles = unsupported_artist_roles


def _upload_group_metadata(**overrides):
    metadata = {
        "title": "Test Album",
        "artists": [("Main Artist", "main"), ("Some Arranger", "arranger")],
        "group_year": 2020,
        "label": "Test Label",
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
    metadata.update(overrides)
    return metadata


def test_compile_data_new_group_keeps_arranger_for_red(monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", False)
    gazelle_site = cast("BaseGazelleApi", cast("object", _FakeGazelleSite("RED")))
    metadata = _upload_group_metadata()

    data = compile_data_new_group(
        gazelle_site=gazelle_site,
        path="/tmp/does-not-exist",
        metadata=metadata,
        track_data={},
        hybrid=True,
        cover_url=None,
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=None,
    )

    assert data["artists[]"] == ["Main Artist", "Some Arranger"]
    assert data["importance[]"] == [1, 8]
    assert len(data["artists[]"]) == len(data["importance[]"])


def test_compile_data_new_group_drops_arranger_for_dic(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cfg.upload.compression, "use_upc_as_catno", False)
    gazelle_site = cast(
        "BaseGazelleApi",
        cast("object", _FakeGazelleSite("DICMusic", unsupported_artist_roles=frozenset({"arranger"}))),
    )
    metadata = _upload_group_metadata(
        artists=[("Main Artist", "main"), ("Some Arranger", "arranger"), ("Guest Artist", "guest")]
    )

    data = compile_data_new_group(
        gazelle_site=gazelle_site,
        path="/tmp/does-not-exist",
        metadata=metadata,
        track_data={},
        hybrid=True,
        cover_url=None,
        spectral_urls=None,
        spectral_ids=None,
        lossy_comment=None,
    )

    assert data["artists[]"] == ["Main Artist", "Guest Artist"]
    assert data["importance[]"] == [1, 2]
    assert len(data["artists[]"]) == len(data["importance[]"])

    captured = capsys.readouterr()
    assert "DICMusic has no Arranger role: not crediting Some Arranger" in captured.out


def test_dic_declares_arranger_unsupported() -> None:
    assert "arranger" in DICApi.unsupported_artist_roles
