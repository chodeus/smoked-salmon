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
