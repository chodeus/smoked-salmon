"""What this fork's checks job adds to upstream's: every dupe match behind a row, and the source the files prove."""

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from test_checks_all_command import fake_flac  # noqa: F401  # pyright: ignore[reportMissingImports]
from test_webui_checks_trackers import (  # noqa: F401  # pyright: ignore[reportMissingImports]
    SEARCHSTRS,
    FakeTracker,
    _album,
    _point_at,
    _run_checks,
    _web,
    configured,
    downloads,
    no_backoff,
    no_learned_secrets,
)

from salmon.webui.jobs import JobManager

TORRENT_PASS = "planted-torrent-pass-4d2e"
# A browse result as a Gazelle site gives it: HTML entities in the names, and a download URL with the torrent pass.
GROUP = {
    "groupId": 77,
    "groupName": "Album &amp; More",
    "artist": "Artist",
    "groupYear": 2019,
    "releaseType": "Album",
    "torrents": [
        {
            "torrentId": 501,
            "format": "FLAC",
            "encoding": "Lossless",
            "media": "WEB",
            "hasLog": False,
            "logScore": 0,
            "remasterTitle": "",
            "remasterYear": 0,
            "remasterRecordLabel": "Label &amp; Co",
            "remasterCatalogueNumber": "CAT-1",
            "seeders": 0,
            "downloadUrl": f"https://tracker.example/torrents.php?action=download&id=501&torrent_pass={TORRENT_PASS}",
        }
    ],
}


def _found() -> web.Response:
    return web.json_response({"status": "success", "response": {"results": [GROUP]}})


def test_a_checks_job_lists_every_dupe_match_through_an_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    downloads: Path,  # noqa: F811
) -> None:
    album = _album(downloads / "Artist - Album (2020) [WEB FLAC]")
    fakes = {"RED": FakeTracker(answers=[_found])}
    _point_at(monkeypatch, fakes)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _run_checks(client, manager, album, ["RED"])
        assert job.result["dupes"] == {
            "RED": {
                "searchstrs": SEARCHSTRS,
                "matches": [
                    {
                        "groupId": 77,
                        "groupName": "Album & More",
                        "artist": "Artist",
                        "groupYear": 2019,
                        "releaseType": "Album",
                        "url": f"{fakes['RED'].url}/torrents.php?id=77",
                        "torrents": [
                            {
                                "torrentId": 501,
                                "format": "FLAC",
                                "encoding": "Lossless",
                                "media": "WEB",
                                "hasLog": False,
                                "logScore": 0,
                                "remasterTitle": "",
                                "remasterYear": 2019,
                                "remasterRecordLabel": "Label & Co",
                                "remasterCatalogueNumber": "CAT-1",
                                "seeders": 0,
                            }
                        ],
                    }
                ],
            }
        }
        assert TORRENT_PASS not in json.dumps(job.result)

    _web(fakes, test)


def test_a_search_that_failed_lists_no_matches(
    monkeypatch: pytest.MonkeyPatch,
    downloads: Path,  # noqa: F811
) -> None:
    album = _album(downloads / "Artist - Album (2020) [WEB FLAC]")
    fakes = {"RED": FakeTracker(answers=[lambda: web.json_response({"status": "failure", "error": "down"})])}
    _point_at(monkeypatch, fakes)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _run_checks(client, manager, album, ["RED"])
        assert job.result["dupes"] == {}

    _web(fakes, test)


def test_a_checks_job_names_the_source_the_files_prove(downloads: Path) -> None:  # noqa: F811
    album = _album(downloads / "Artist - Album (2020) [CD FLAC]")
    (album / "Album.log").write_text("Exact Audio Copy V1.8 from 1. July 2024\n")
    unproven = _album(downloads / "Artist - Album (2020) [FLAC]")

    async def test(client: TestClient, manager: JobManager) -> None:
        assert (await _run_checks(client, manager, album, [])).result["detected_source"] == "CD"
        assert (await _run_checks(client, manager, unproven, [])).result["detected_source"] is None

    _web({}, test)
