"""``salmon up --skip-search``: the metadata of --source-url, without the stores' search."""

import re
from copy import deepcopy
from typing import Any

import anyio
import asyncclick as click
import pytest
from asyncclick.testing import CliRunner
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    SOURCE_URL,
    _album,
    _loose_rate_limit,  # noqa: F401 (a fixture: see the test using it)
    _run_up,
)

import salmon.trackers
import salmon.uploader as uploader
from salmon import cfg
from salmon.errors import ScrapeError
from salmon.tagger import metadata as metadata_mod

QOBUZ_URL = "https://open.qobuz.com/album/0887396827479"


def make_release_data() -> dict:
    return {
        "artists": [("Example Artist", "main")],
        "title": "Example Release",
        "group_year": "2026",
        "year": "2026",
        "date": None,
        "edition_title": None,
        "label": None,
        "catno": None,
        "rls_type": None,
        "genres": ["Electronic"],
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "scene": False,
        "source": "WEB",
        "cover": None,
        "upc": None,
        "comment": None,
        "urls": [],
        "tracks": {
            "1": {
                "1": {
                    "track#": "1",
                    "disc#": "1",
                    "tracktotal": "1",
                    "disctotal": "1",
                    "artists": [("Example Artist", "main")],
                    "title": "Track One",
                    "replay_gain": None,
                    "peak": None,
                    "isrc": None,
                    "explicit": None,
                    "format": None,
                    "streamable": None,
                }
            }
        },
    }


class QobuzScraper:
    regex = re.compile(r"https://open\.qobuz\.com/album/")

    async def scrape_release(self, url: str):
        data = deepcopy(make_release_data())
        data["title"] = "Qobuz Title"
        data["urls"] = [url]
        return data


class QobuzSource:
    Scraper = QobuzScraper


class FailingScraper:
    regex = QobuzScraper.regex

    async def scrape_release(self, url: str):
        raise ScrapeError("boom")


class FailingSource:
    Scraper = FailingScraper


@pytest.fixture
def searches(monkeypatch) -> list[str]:
    """What the stores' search and the prompt were asked: nothing, when the search is skipped."""
    asked: list[str] = []

    async def fake_search(*_args, **_kwargs):
        asked.append("search")
        return {}

    async def fake_select(_choices, _rls_data, default=None):
        asked.append("select")
        return {"upc": "1", "catno": None, "tracks": {}, "genres": [], "title": "Searched"}, None

    monkeypatch.setattr(metadata_mod, "run_metasearch", fake_search)
    monkeypatch.setattr(metadata_mod, "_select_choice", fake_select)
    monkeypatch.setattr(metadata_mod, "METASOURCES", {"Qobuz": QobuzSource})
    monkeypatch.setattr(metadata_mod, "tag_urls", lambda _path: ([], []))
    return asked


def _get(url, skip_search, rls_data=None):
    async def run():
        return await metadata_mod.get_metadata(
            "/nowhere", {"01.flac": {}}, rls_data or make_release_data(), skip_search=skip_search, skip_search_url=url
        )

    return anyio.run(run)


def test_skip_search_scrapes_the_url_and_never_searches(searches) -> None:
    metadata, source_url = _get(QOBUZ_URL, True)

    assert searches == []
    assert source_url == QOBUZ_URL
    assert metadata["title"] == "Qobuz Title"


def test_skip_search_without_a_url_prints_why_and_searches(searches, capsys) -> None:
    metadata, source_url = _get(None, True)

    assert searches == ["search", "select"]
    assert "--skip-search needs --source-url" in capsys.readouterr().out
    assert metadata["title"] == "Searched"
    assert source_url is None


def test_skip_search_with_an_unsupported_url_prints_why_and_searches(searches, capsys) -> None:
    _get("https://example.com/some/album", True)

    assert searches == ["search", "select"]
    assert "no metadata source scrapes https://example.com/some/album" in capsys.readouterr().out


def test_without_skip_search_the_url_is_ignored_and_the_search_runs(searches, capsys) -> None:
    _get(QOBUZ_URL, False)

    assert searches == ["search", "select"]
    assert "--skip-search" not in capsys.readouterr().out


def test_a_failed_scrape_falls_back_to_the_search(searches, monkeypatch, capsys) -> None:
    monkeypatch.setattr(metadata_mod, "METASOURCES", {"Qobuz": FailingSource})
    rls_data = make_release_data()

    metadata, _source_url = _get(QOBUZ_URL, True, rls_data)

    assert searches == ["search", "select"]
    assert "Scrape error" in capsys.readouterr().out
    assert metadata["title"] == "Searched"
    assert QOBUZ_URL not in rls_data["urls"]


@pytest.mark.parametrize(("args", "expected"), [([], False), (["--skip-search"], True)])
def test_the_flag_reaches_the_upload(monkeypatch, tmp_path, args, expected) -> None:
    # The fork's up calls upload itself: upstream's UpOptions and run_up come with salmon web's upload page.
    seen: list[Any] = []

    async def fake_upload(*_args, **kwargs) -> None:
        seen.append(kwargs)

    async def invoke():
        return await CliRunner().invoke(uploader.up, [str(tmp_path), "-t", "RED", "-s", "WEB", "-su", QOBUZ_URL, *args])

    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED"])
    monkeypatch.setattr(uploader, "upload", fake_upload)
    result = anyio.run(invoke)

    assert result.exit_code == 0, result.output
    assert [(k["skip_search"], k["source_url"]) for k in seen] == [(expected, QOBUZ_URL)]


@pytest.mark.usefixtures("_loose_rate_limit")
@pytest.mark.parametrize(("args", "expected"), [((), False), (("--skip-search",), True)])
def test_the_run_passes_the_flag_and_source_url_to_the_metadata_step(monkeypatch, tmp_path, args, expected) -> None:
    # Through the fork's real up, upload and _upload_staged; the run stops at the metadata step.
    seen: list[tuple[bool, str | None]] = []

    async def get_metadata(_path, _tags, _rls_data, *, skip_search_url=None, skip_search=False):
        seen.append((skip_search, skip_search_url))
        raise click.Abort

    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    run = _run_up(monkeypatch, _album(tmp_path / "album"), torrents, args, get_metadata=get_metadata)

    assert run.result.exit_code == 0, run.result.output
    assert seen == [(expected, SOURCE_URL)]
