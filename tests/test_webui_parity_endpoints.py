"""HTTP surface for the CLI commands the web interface exposes.

Covers request validation and wiring, not the underlying command behaviour —
those have their own tests.
"""

import os
import time

import pytest
from fastapi.testclient import TestClient

import salmon.trackers
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
        "check",
        "checkconf",
        "checkspecs",
        "compress",
        "cross-upload",
        "descgen",
        "downconv",
        "health",
        "images",
        "meta",
        "metas",
        "specs",
        "tag",
        "transcode",
        "up",
        "web",
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


def test_cross_upload_refuses_an_outside_path_whether_or_not_it_exists(client, two_trackers) -> None:
    """Confinement must not depend on an existence probe, which would say what is on the host."""
    source, target = two_trackers
    body = {"path": "/nonexistent-salmon-zzz/album", "source": source, "target": target}
    assert client.post("/api/cross-upload", json=body).status_code == 403


def test_cross_upload_still_accepts_a_source_url(client, two_trackers) -> None:
    source, target = two_trackers
    body = {"path": "https://example.com/album/1", "source": source, "target": target}
    assert client.post("/api/cross-upload", json=body).status_code != 403


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


def test_checkconf_is_cached_so_dashboard_loads_do_not_hammer_trackers(client, monkeypatch) -> None:
    # The dashboard calls this on every load; each real call costs two live
    # requests per tracker, so repeat calls must be served from cache.
    from salmon.webui.routers import system

    calls = []

    async def fake_check(code):
        calls.append(code)
        return {
            "tracker": code,
            "session_ok": True,
            "session_error": None,
            "api_key_configured": False,
            "api_key_ok": None,
            "api_key_error": None,
        }

    monkeypatch.setattr(system, "check_tracker_connection", fake_check)
    monkeypatch.setitem(system._checkconf_cache, "at", 0.0)
    monkeypatch.setitem(system._checkconf_cache, "result", None)

    first = client.post("/api/checkconf").json()
    assert first["cached"] is False
    hits_after_first = len(calls)
    assert hits_after_first > 0

    second = client.post("/api/checkconf").json()
    assert second["cached"] is True
    assert len(calls) == hits_after_first, "cached call must not touch the trackers"

    forced = client.post("/api/checkconf?force=true").json()
    assert forced["cached"] is False
    assert len(calls) > hits_after_first, "force=true must bypass the cache"


def test_concurrent_checkconf_misses_share_one_probe(client, monkeypatch) -> None:
    """Several dashboards loading at once must not each start their own tracker probe."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from salmon.webui.routers import system

    rounds = []

    async def fake_check(code):
        rounds.append(code)
        await asyncio.sleep(0.05)  # long enough for the other requests to pile up
        return {
            "tracker": code,
            "session_ok": True,
            "session_error": None,
            "api_key_configured": False,
            "api_key_ok": None,
            "api_key_error": None,
        }

    monkeypatch.setattr(system, "check_tracker_connection", fake_check)
    monkeypatch.setitem(system._checkconf_cache, "at", 0.0)
    monkeypatch.setitem(system._checkconf_cache, "result", None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        bodies = [f.result().json() for f in [pool.submit(client.post, "/api/checkconf") for _ in range(4)]]

    import salmon.trackers

    assert len(rounds) == len(salmon.trackers.tracker_list), "the probe must run exactly once"
    assert sum(1 for b in bodies if b["cached"] is False) == 1
    assert all(b["ok"] for b in bodies)


def test_health_reports_disk_usage_for_each_directory(client) -> None:
    """The dashboard draws a usage bar from these, so every entry must carry the fields."""
    body = client.get("/api/health").json()
    assert set(body["directories"]) == {"download", "tmp", "dottorrents"}
    for name, info in body["directories"].items():
        assert set(info) == {"path", "exists", "free_bytes", "total_bytes"}, name
        if info["exists"]:
            assert info["total_bytes"] and info["total_bytes"] > 0, name
            assert 0 <= info["free_bytes"] <= info["total_bytes"], name


def test_dir_info_handles_a_missing_directory() -> None:
    from salmon.webui.routers.system import _dir_info

    info = _dir_info("/definitely/not/a/real/path")
    assert info == {"path": "/definitely/not/a/real/path", "exists": False, "free_bytes": None, "total_bytes": None}
    assert _dir_info(None)["exists"] is False


def test_empty_check_selection_is_not_reported_as_running_everything(client, album_dir) -> None:
    """`checks: []` skips the file checks, so the job title must not claim they ran."""
    resp = client.post("/api/checks/run", json={"path": str(album_dir), "checks": []})
    assert resp.status_code == 200
    assert resp.json()["title"].startswith("Checks (no file checks):")

    omitted = client.post("/api/checks/run", json={"path": str(album_dir)})
    assert "integrity" in omitted.json()["title"]


def _wait(client, job_id, tries=200):
    for _ in range(tries):
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in {"done", "error", "cancelled"}:
            return body
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def _capture_upload(monkeypatch):
    """Stub run_upload and return the dict its kwargs land in."""
    from salmon.webui.routers import upload as upload_router

    seen: dict = {}

    async def fake_upload(*args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)

    monkeypatch.setattr(upload_router, "run_upload", fake_upload)
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    return seen


def test_upload_forwards_the_selected_trackers(client, album_dir, monkeypatch) -> None:
    """Without this, run_upload offers every configured site for continuation —
    so picking OPS could still offer RED."""
    seen = _capture_upload(monkeypatch)
    resp = client.post(
        "/api/upload",
        json={"path": str(album_dir), "tracker": "RED", "trackers": ["RED", "OPS"], "source": "WEB"},
    )
    assert resp.status_code == 200
    _wait(client, resp.json()["id"])
    assert seen["trackers"] == ["RED", "OPS"]


def test_upload_omitting_trackers_keeps_the_cli_behaviour(client, album_dir, monkeypatch) -> None:
    seen = _capture_upload(monkeypatch)
    resp = client.post("/api/upload", json={"path": str(album_dir), "tracker": "RED", "source": "WEB"})
    assert resp.status_code == 200
    _wait(client, resp.json()["id"])
    assert seen["trackers"] is None, "an empty selection must mean 'offer everything', as the CLI does"


def test_upload_rejects_an_unknown_tracker_in_the_list(client, album_dir, monkeypatch) -> None:
    _capture_upload(monkeypatch)
    resp = client.post(
        "/api/upload",
        json={"path": str(album_dir), "tracker": "RED", "trackers": ["RED", "NOPE"], "source": "WEB"},
    )
    assert resp.status_code == 422
    assert "NOPE" in resp.json()["detail"]
