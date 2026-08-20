"""HTTP surface for the CLI commands the web interface exposes.

Covers request validation and wiring, not the underlying command behaviour —
those have their own tests.
"""

import os

import pytest
from fastapi.testclient import TestClient

from salmon import cfg
from salmon.webui.app import create_app
from salmon.webui.jobs import manager


@pytest.fixture(autouse=True)
def _clean():
    manager.jobs.clear()
    manager._subscribers.clear()
    manager._active_lock_keys.clear()


@pytest.fixture
def client():
    with TestClient(create_app(), base_url="http://localhost") as c:
        yield c


@pytest.fixture
def album(tmp_path):
    base = os.path.realpath(cfg.directory.download_directory)
    path = os.path.join(base, tmp_path.name, "Artist - Album")
    os.makedirs(path, exist_ok=True)
    return path


def test_options_expose_every_choice_the_cli_has(client):
    body = client.get("/api/tools/options").json()
    assert body["encodings"], "encoding choices must be offered"
    assert "320" in body["transcodes"] and "V0" in body["transcodes"]
    assert body["image_hosts"], "image hosts must be listed"
    assert body["sources"] and body["trackers"]


def test_upload_options_include_encodings(client):
    body = client.get("/api/upload/options").json()
    assert "encodings" in body and body["encodings"]


def test_upload_rejects_essential_only_with_scene(client, album):
    # Mirrors the CLI, where the two flags are mutually exclusive.
    r = client.post(
        "/api/upload",
        json={"path": album, "tracker": "RED", "essential_only": True, "scene": True},
    )
    assert r.status_code == 422
    assert "essential_only" in r.json()["detail"]


def test_upload_rejects_unknown_encoding(client, album):
    r = client.post("/api/upload", json={"path": album, "tracker": "RED", "encoding": "V9"})
    assert r.status_code == 422
    assert "encoding" in r.json()["detail"].lower()


def test_cross_upload_rejects_same_source_and_target(client, album):
    r = client.post("/api/cross-upload", json={"path": album, "source": "RED", "target": "RED"})
    assert r.status_code == 422
    assert "differ" in r.json()["detail"]


def test_cross_upload_rejects_unknown_tracker(client, album):
    r = client.post("/api/cross-upload", json={"path": album, "source": "RED", "target": "NOPE"})
    assert r.status_code == 422


def test_tag_rejects_unknown_source(client, album):
    r = client.post("/api/tag", json={"path": album, "source": "Betamax"})
    assert r.status_code == 422


def test_tag_refuses_a_path_outside_the_roots(client):
    r = client.post("/api/tag", json={"path": "/etc", "source": "CD"})
    assert r.status_code == 403


def test_image_upload_refuses_files_outside_the_roots(client):
    r = client.post("/api/images/upload", json={"paths": ["/etc/hosts"]})
    assert r.status_code == 403


def test_image_upload_rejects_unknown_host(client, album):
    r = client.post("/api/images/upload", json={"paths": [album], "host": "nosuchhost"})
    assert r.status_code == 422


def test_descgen_requires_at_least_one_url(client):
    assert client.post("/api/descgen", json={"urls": []}).status_code == 422


def test_compress_refuses_a_path_outside_the_roots(client):
    r = client.post("/api/convert/compress", json={"path": "/etc"})
    assert r.status_code == 403


def test_cli_still_exposes_every_command_after_the_refactor() -> None:
    # checkconf and descgen were rewired onto shared helpers; the commands must survive.
    from salmon.common import commandgroup

    names = set(commandgroup.commands)
    expected = {
        "check", "checkconf", "checkspecs", "compress", "cross-upload", "descgen",
        "downconv", "health", "images", "meta", "metas", "specs", "tag",
        "transcode", "up", "web",
    }
    assert expected <= names, f"missing CLI commands: {sorted(expected - names)}"


def test_every_cli_command_has_a_web_equivalent() -> None:
    """The web interface should not lag the CLI. Update both lists together.

    Reads the OpenAPI schema rather than probing HTTP: the SPA catch-all serves
    index.html for unknown /api paths, so a 404 probe cannot detect a missing route.
    """
    with TestClient(create_app(dev=True), base_url="http://localhost") as dev:
        paths = set(dev.get("/api/openapi.json").json()["paths"])

    for path in (
        "/api/upload",  # up
        "/api/checks/run",  # check
        "/api/checkconf",  # checkconf
        "/api/spectrals/generate",  # specs
        "/api/spectrals/upload",  # checkspecs
        "/api/convert/transcode",  # transcode
        "/api/convert/downconvert",  # downconv
        "/api/convert/compress",  # compress
        "/api/search",  # metas
        "/api/metadata",  # meta
        "/api/health",  # health
        "/api/descgen",  # descgen
        "/api/images/upload",  # images
        "/api/tag",  # tag
        "/api/cross-upload",  # cross-upload
    ):
        assert path in paths, f"no web equivalent for {path}"


def test_compress_refuses_a_read_only_library_source(client, tmp_path, monkeypatch) -> None:
    # Recompression rewrites FLACs in place, so it must not touch a library_dirs entry.
    base = os.path.realpath(cfg.directory.download_directory)
    lib = os.path.join(base, tmp_path.name, "library")
    album = os.path.join(lib, "Artist - Album")
    os.makedirs(album, exist_ok=True)
    monkeypatch.setattr(cfg.directory, "library_dirs", [lib])

    assert client.post("/api/convert/compress", json={"path": album}).status_code == 403


def test_offered_sources_match_the_canonical_set(client) -> None:
    """The web form must not offer a source the tagger will later reject.

    The hand-maintained copy this replaced had drifted to include "Blu-Ray",
    so a job would start, do real work, then die on InvalidMetadataError.
    """
    from salmon.constants import SOURCES as canonical

    for endpoint in ("/api/upload/options", "/api/tools/options"):
        offered = set(client.get(endpoint).json()["sources"])
        assert offered == set(canonical.values()), f"{endpoint} drifted from salmon.constants.SOURCES"


def test_upload_rejects_a_source_the_tagger_would_reject(client, album) -> None:
    assert client.post("/api/upload", json={"path": album, "tracker": "RED", "source": "Blu-Ray"}).status_code == 422


def test_descgen_caps_the_number_of_urls(client) -> None:
    from salmon.webui.routers.tools import MAX_DESCGEN_URLS

    too_many = [f"https://example.com/{i}" for i in range(MAX_DESCGEN_URLS + 1)]
    assert client.post("/api/descgen", json={"urls": too_many}).status_code == 422


def test_descgen_refuses_internal_addresses(client) -> None:
    # SSRF: the scrapers match arbitrary hosts, so an internal URL would be fetched.
    for url in ("http://127.0.0.1/album", "http://10.0.20.11/album", "file:///etc/passwd"):
        r = client.post("/api/descgen", json={"urls": [url]})
        assert r.status_code == 422, f"{url} should be refused"


@pytest.fixture
def two_trackers(monkeypatch):
    """cross-upload needs two distinct trackers; the test config may configure one."""
    import salmon.trackers

    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    return "RED", "OPS"


def test_cross_upload_refuses_a_local_path_outside_the_roots(client, two_trackers) -> None:
    source, target = two_trackers
    r = client.post("/api/cross-upload", json={"path": "/etc", "source": source, "target": target})
    assert r.status_code == 403


def test_cross_upload_still_accepts_a_torrent_id(client, two_trackers) -> None:
    # IDs and URLs are not filesystem paths and must pass the confinement check.
    source, target = two_trackers
    r = client.post("/api/cross-upload", json={"path": "12345", "source": source, "target": target})
    assert r.status_code != 403


@pytest.mark.parametrize("bad", [0, -1])
def test_upload_rejects_non_positive_spectral_tracks(client, album, bad) -> None:
    # Negative numbers index from the end of the track list; 0 is a sentinel.
    r = client.post("/api/upload", json={"path": album, "tracker": "RED", "spectrals": [bad]})
    assert r.status_code == 422
