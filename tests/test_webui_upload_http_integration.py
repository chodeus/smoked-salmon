"""End-to-end tests for the web upload wizard with the REAL tracker HTTP layer.

A variant of test_webui_upload_wizard.py that does not mock the tracker at the
object seam: the fake Gazelle server from tests/fake_gazelle.py runs in-process
on an ephemeral port (aiohttp AppRunner on the test's event loop) and a real
salmon.trackers.red.RedApi talks to it over TCP. Cookie/API-key auth headers,
the tenacity retry decorator, the aiolimiter rate limiter, multipart form
composition and JSON decoding in BaseGazelleApi._request/api_call/upload all
execute for real — no mocks of api_call/_request/upload.

Threading model: the upload job runs in a JobManager worker thread with its
own event loop while the fake server serves on the test loop; the two only
meet through a real TCP socket. The RedApi is instantiated INSIDE the job
thread (see make_red_api / http_wizard_factory) so its per-instance
AsyncLimiter binds to the job loop, mirroring tools/dev_web_with_fake_tracker.py
— but as instance attributes only, never class-level mutation.

The non-tracker world (metadata providers, tagger plumbing, spectral
*generation*, image hosts) stays mocked exactly like in the object-seam E2E
test; real providers would be flaky in CI.
"""

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from fake_gazelle import UPLOADS, make_fake_gazelle
from torf import Torrent

import salmon.trackers
from salmon import cfg
from salmon.trackers.red import RedApi
from salmon.uploader import upload as run_upload
from salmon.uploader.upload import concat_track_data, prepare_and_upload
from salmon.webui.interaction import install_interaction_patches
from salmon.webui.jobs import JobManager

POLL = 0.005
DEADLINE = 15.0

# ---------------------------------------------------------------------------
# Helpers (poll loops without fixed sleeps; same as the object-seam E2E test)
# ---------------------------------------------------------------------------


async def join_job(job, timeout=DEADLINE):
    """Wait until the job finished AND its worker thread exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.finished_at is not None and (job.thread is None or not job.thread.is_alive()):
            return
        await asyncio.sleep(POLL)
    raise AssertionError(
        f"job {job.id} never finished: status={job.status} error={job.error!r} question={job.question}"
    )


async def drive(job, script, timeout=DEADLINE):
    """Answer the job's questions per script: [(text substring, answer), ...].

    Asserts the exact question sequence; an unmatched or extra question fails
    the test with the question text in the message. Returns the question
    dicts seen, in order. While this poll loop yields, the test's event loop
    keeps serving the fake Gazelle HTTP requests made by the job thread.
    """
    seen = []
    last_id = None
    idx = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.finished_at is not None:
            break
        question = job.question
        if question is None or question["id"] == last_id:
            await asyncio.sleep(POLL)
            continue
        if idx >= len(script):
            raise AssertionError(f"Unscripted question: {question['kind']}: {question['text']!r}")
        expected, answer = script[idx]
        if expected not in question["text"]:
            raise AssertionError(
                f"Question {idx} mismatch: expected substring {expected!r} but the job asked {question['text']!r}"
            )
        seen.append(question)
        last_id = question["id"]
        assert job.interaction.answer(question["id"], answer) is True
        idx += 1
    else:
        raise AssertionError(
            f"Timed out after answering {idx}/{len(script)} questions: "
            f"status={job.status} error={job.error!r} question={job.question}"
        )
    if idx != len(script):
        raise AssertionError(
            f"Job finished after only {idx}/{len(script)} questions: status={job.status} error={job.error!r}"
        )
    return seen


# ---------------------------------------------------------------------------
# Fixtures: interaction patches, job managers, in-process fake Gazelle server
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _interaction_patches():
    install_interaction_patches()  # idempotent


@pytest.fixture
def jm():
    """Factory for isolated JobManagers; cancels/joins leftover jobs on teardown."""
    managers = []

    def make() -> JobManager:
        m = JobManager()
        managers.append(m)
        return m

    yield make
    for m in managers:
        for job in list(m.jobs.values()):
            if job.finished_at is None:
                m.cancel(job.id)
        for job in m.jobs.values():
            if job.thread is not None:
                job.thread.join(timeout=5)


@pytest.fixture
async def start_gazelle():
    """Factory: serve a fake Gazelle web.Application on an ephemeral port.

    The server runs on the test's event loop; the job thread reaches it over
    real TCP. Returns the base URL (no trailing slash). Middlewares may be
    appended to the app before calling this. All runners are cleaned up on
    teardown.
    """
    runners = []

    async def start(app: web.Application) -> str:
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = runner.addresses[0][1]
        return f"http://127.0.0.1:{port}"

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.fixture
def dot_torrents_dir(tmp_path):
    d = tmp_path / "dottorrents"
    d.mkdir()
    return str(d)


# ---------------------------------------------------------------------------
# The mocked (non-tracker) world — copied from the object-seam E2E test
# ---------------------------------------------------------------------------

TRACK_FILES = ["01. Intro.flac", "02. Mittelteil.flac", "03. Outro.flac"]
TRACK_TITLES = ["Intro", "Mittelteil", "Outro"]

SEARCH_RESULT = {
    "groupId": 555,
    "artist": "Testartist",
    "groupName": "Testalbum",
    "groupYear": 2024,
    "releaseType": "Album",
    "tags": ["electronic"],
}


def make_tag(tracknumber, title):
    return SimpleNamespace(
        album="Testalbum",
        date="2024-03-01",
        upc=None,
        label="Test Label",
        catno="TL-001",
        genre=["Electronic"],
        artist=["Testartist"],
        title=title,
        tracknumber=str(tracknumber),
        discnumber="1",
        tracktotal="3",
        disctotal="1",
        replay_gain=None,
        peak=None,
        isrc=None,
        conductor=None,
        composer=None,
    )


def make_fake_tags():
    return {fn: make_tag(i, title) for i, (fn, title) in enumerate(zip(TRACK_FILES, TRACK_TITLES, strict=True), 1)}


def make_audio_info():
    return {
        fn: {"duration": 180 + i, "sample rate": 44100, "bit rate": 941000, "precision": 16}
        for i, fn in enumerate(TRACK_FILES)
    }


def make_web_metadata():
    """What the (stubbed) metadata sources return; feeds the real form compiler."""
    return {
        "title": "Testalbum",
        "artists": [("Testartist", "main")],
        "group_year": 2024,
        "year": 2024,
        "edition_title": None,
        "label": "Test Label",
        "catno": "TL-001",
        "upc": None,
        "rls_type": "Album",
        "genres": ["Electronic"],
        "format": "FLAC",
        "encoding": "Lossless",
        "encoding_vbr": False,
        "source": "WEB",
        "scene": False,
        "comment": None,
        "urls": ["https://example.com/testalbum"],
        "date": "2024-03-01",
        "cover": "https://covers.example/testalbum.jpg",
    }


@pytest.fixture
def upload_world(monkeypatch, tmp_path):
    """Stub upload()'s external seams; pin config so the flow is deterministic
    regardless of the ambient developer config.toml. The tracker seam is NOT
    stubbed — that is the point of this module."""
    world = SimpleNamespace(
        tags=make_fake_tags(),
        audio_info=make_audio_info(),
        metadata=make_web_metadata(),
        spectral_uploads=[],
    )

    monkeypatch.setattr("salmon.uploader.gather_audio_info", lambda path: dict(world.audio_info))
    monkeypatch.setattr("salmon.uploader.standardize_tags", lambda path: None)
    monkeypatch.setattr("salmon.uploader.gather_tags", lambda path: dict(world.tags))

    async def fake_check_tags(path):
        return dict(world.tags)

    async def fake_get_metadata(path, tags, rls_data):
        return dict(world.metadata), None

    async def fake_review_metadata_with_ai(metadata, *args, **kwargs):
        return metadata

    async def fake_check_folder_structure(path, scene, essential_only=False):
        return None

    async def fake_download_cover(path, cover_url):
        return os.path.join(path, "cover.jpg"), False

    async def fake_upload_cover(cover_path):
        return "https://img.example/cover"

    async def fake_generate_spectrals_all(path, spectrals_path, audio_info):
        for idx in range(1, len(TRACK_FILES) + 1):
            for kind in ("Full", "Zoom"):
                (Path(spectrals_path) / f"{idx:02d} {kind}.png").write_bytes(b"png")
        return {i + 1: fn for i, fn in enumerate(TRACK_FILES)}

    async def fake_handle_spectrals(spectrals_path, spectral_ids, delete_spectrals=True):
        world.spectral_uploads.append((spectrals_path, dict(spectral_ids or {})))
        return {sid: [f"https://img.example/{sid}f", f"https://img.example/{sid}z"] for sid in (spectral_ids or {})}

    monkeypatch.setattr("salmon.uploader.check_tags", fake_check_tags)
    monkeypatch.setattr("salmon.uploader.get_metadata", fake_get_metadata)
    monkeypatch.setattr("salmon.uploader.review_metadata_with_ai", fake_review_metadata_with_ai)
    monkeypatch.setattr("salmon.uploader.tag_files", lambda *a, **k: None)
    monkeypatch.setattr("salmon.uploader.rename_files", lambda *a, **k: None)
    monkeypatch.setattr("salmon.uploader.rename_folder", lambda path, metadata, auto_rename: path)
    monkeypatch.setattr("salmon.uploader.check_folder_structure", fake_check_folder_structure)
    monkeypatch.setattr("salmon.uploader.download_cover_if_nonexistent", fake_download_cover)
    monkeypatch.setattr("salmon.uploader.upload_cover", fake_upload_cover)
    monkeypatch.setattr("salmon.uploader.spectrals.generate_spectrals_all", fake_generate_spectrals_all)
    monkeypatch.setattr("salmon.uploader.handle_spectrals_upload_and_deletion", fake_handle_spectrals)

    tmp_dir = tmp_path / "salmon-tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_dir))
    monkeypatch.setattr(cfg.upload, "yes_all", False)
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", True)
    monkeypatch.setattr(cfg.upload, "upload_to_seedbox", False)
    monkeypatch.setattr(cfg.upload, "debug_tracker_connection", False)
    monkeypatch.setattr(cfg.upload.requests, "check_requests", False)
    monkeypatch.setattr(cfg.upload.requests, "last_minute_dupe_check", False)
    monkeypatch.setattr(cfg.upload.description, "copy_uploaded_url_to_clipboard", False)
    monkeypatch.setattr(cfg.image, "remove_auto_downloaded_cover_image", False)
    monkeypatch.setattr(cfg.image, "auto_compress_cover", False)
    monkeypatch.setattr(cfg.image, "default_spectral_ids", None)
    monkeypatch.setattr(cfg, "seedbox", [])
    # RED + one remaining site so the "upload to another tracker?" loop runs.
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS"])
    return world


# ---------------------------------------------------------------------------
# Real RedApi wiring + job factory
# ---------------------------------------------------------------------------


def make_red_api(base_url: str, torrents_dir: str) -> RedApi:
    """A real RedApi pointed at the in-process fake Gazelle server.

    Must be called inside the job thread: BaseGazelleApi's AsyncLimiter binds
    to the first event loop that uses it, and threaded jobs run on their own
    loop. URLs and credentials are instance attributes, exactly like
    tools/dev_web_with_fake_tracker.py wires them.
    """
    api = RedApi()
    api.base_url = base_url
    api.tracker_url = base_url
    api.cookie = "fake-session"
    api.api_key = "fake-api-key"
    api.dot_torrents_dir = torrents_dir
    return api


def http_wizard_factory(base_url, torrents_dir, path):
    """Job factory calling the unmodified pipeline like routers/upload.py does,
    with the real RedApi created in the job thread (not the server loop)."""

    async def run(job):
        gazelle_site = make_red_api(base_url, torrents_dir)
        await run_upload(
            gazelle_site,
            path,
            None,  # group_id
            None,  # source -> asked interactively
            None,  # lossy -> asked interactively
            (),  # spectral ids -> asked interactively
            None,  # encoding -> derived from (fake) 16bit FLAC audio info
            source_url=None,
            request_id=None,
            spectrals_after=False,
            auto_rename=False,
            skip_up=True,
            skip_mqa=True,
            skip_log_check=True,
            skip_integrity_check=True,
        )
        return {"album_path": path, "tracker": gazelle_site.site_code}

    return run


HAPPY_PATH_SCRIPT = [
    ("What is the source of this release?", "web"),
    ("Would you like to upload to an existing group?", "n"),
    ("Is this release lossy mastered?", "n"),
    ("What spectral IDs would you like to upload", "*"),
    ("Would you like to upload the torrent?", True),
    ("Would you like to check downconversion options?", False),
    ("Your choices are OPS or [n]one.", "n"),
]


# ---------------------------------------------------------------------------
# 1. Happy path over real HTTP
# ---------------------------------------------------------------------------


async def test_happy_path_uploads_over_real_http(jm, album_dir, upload_world, start_gazelle, dot_torrents_dir):
    app = make_fake_gazelle(browse_results=[dict(SEARCH_RESULT)])
    base_url = await start_gazelle(app)

    m = jm()
    path = str(album_dir)
    job = m.create_threaded(
        "upload", "Upload to RED", http_wizard_factory(base_url, dot_torrents_dir, path), lock_key=path
    )

    await drive(job, HAPPY_PATH_SCRIPT)
    await join_job(job)

    assert job.status == "done", job.error
    assert job.error is None
    assert job.result == {"album_path": path, "tracker": "RED"}
    assert path not in m._active_lock_keys

    # Exactly one multipart POST reached the fake server through the real
    # aiohttp layer, carrying the compiled form fields, the authkey from the
    # real authenticate round, and the freshly generated .torrent file.
    assert len(app[UPLOADS]) == 1
    fields = app[UPLOADS][0]["fields"]
    assert fields["title"] == "Testalbum"
    assert fields["artists[]"] == "Testartist"
    assert fields["year"] == "2024"
    assert fields["format"] == "FLAC"
    assert fields["bitrate"] == "Lossless"
    assert fields["media"] == "WEB"
    assert fields["auth"] == "fake-authkey"
    assert "groupid" not in fields  # new group upload
    assert app[UPLOADS][0]["files"] == ["meowmeow.torrent"]

    # Real torf torrent on disk; its announce is built from the passkey the
    # real ajax.php?action=index authenticate round returned over HTTP, and
    # upload_and_report rewrote it with the permalink comment.
    torrent_path = os.path.join(dot_torrents_dir, f"{album_dir.name} - RED.torrent")
    assert os.path.isfile(torrent_path)
    read_back = Torrent.read(torrent_path)
    announce_urls = [url for tier in read_back.trackers for url in tier]
    assert announce_urls == [f"{base_url}/fake-passkey/announce"]
    assert "fake-passkey" in announce_urls[0]
    assert read_back.private is True
    assert read_back.source == "RED"
    assert read_back.comment == f"{base_url}/torrents.php?torrentid=424242"

    # Spectrals surfaced to the browser; the selected ids reached the
    # (stubbed) spectral uploader.
    assert job.interaction.spectrals is not None
    assert len(job.interaction.spectrals["files"]) == 6
    assert upload_world.spectral_uploads == [
        (job.interaction.spectrals["path"], {1: TRACK_FILES[0], 2: TRACK_FILES[1], 3: TRACK_FILES[2]})
    ]

    log = "\n".join(job.log_lines)
    assert "Uploading to a new torrent group." in log
    assert f"Successfully uploaded {base_url}/torrents.php?torrentid=424242" in log
    assert "Done with this release." in log


# ---------------------------------------------------------------------------
# 2. Site rejects the upload -> RequestError from the real HTTP layer
# ---------------------------------------------------------------------------


async def test_upload_rejection_propagates_request_error_to_job(jm, album_dir, start_gazelle, dot_torrents_dir):
    """action=upload answered with {"status": "failure", ...} -> the real HTTP
    layer (api_key_upload) raises RequestError with the site's message and
    the job ends status='error' mentioning 'already uploaded'.

    Deviation note: the full upload() wizard deliberately catches RequestError
    per tracker (`except RequestError` in salmon/uploader/__init__.py) so its
    multi-tracker loop can carry on — at job level that path ends 'done' with
    the failure only in the log. To pin genuine error propagation through the
    real HTTP stack, this job runs the real prepare_and_upload step (form
    compile -> HTTP authenticate -> torf torrent -> multipart POST), where
    the RequestError surfaces uncaught and errors the job.
    """
    app = make_fake_gazelle(browse_results=[dict(SEARCH_RESULT)])
    rejections = {"count": 0}

    @web.middleware
    async def reject_upload(request, handler):
        if request.method == "POST" and request.query.get("action") == "upload":
            rejections["count"] += 1
            return web.json_response({"status": "failure", "error": "You already uploaded this"})
        return await handler(request)

    app.middlewares.append(reject_upload)
    base_url = await start_gazelle(app)

    metadata = make_web_metadata()
    metadata["tags"] = "Electronic"  # normally set by upload() before prepare_and_upload
    track_data = concat_track_data(make_fake_tags(), make_audio_info())
    path = str(album_dir)

    async def factory(job):
        gazelle_site = make_red_api(base_url, dot_torrents_dir)
        await prepare_and_upload(
            gazelle_site=gazelle_site,
            path=path,
            group_id=None,
            metadata=metadata,
            cover_url="https://img.example/cover",
            track_data=track_data,
            hybrid=False,
            lossy_master=False,
            spectral_urls=None,
            spectral_ids=None,
            lossy_comment=None,
            request_id=None,
            source_url=None,
        )
        return "unreachable"

    m = jm()
    job = m.create_threaded("upload", "Upload to RED", factory, lock_key=path)
    await join_job(job)

    assert job.status == "error"
    assert job.error is not None
    assert job.error.startswith("RequestError")
    assert "already uploaded" in job.error
    assert job.result is None
    assert rejections["count"] == 1  # the POST really went over the wire
    assert app[UPLOADS] == []  # and the site recorded nothing
    assert path not in m._active_lock_keys


# ---------------------------------------------------------------------------
# 3. index 500s once -> tenacity retries and the flow still completes
# ---------------------------------------------------------------------------


async def test_index_500_is_retried_and_flow_completes(jm, album_dir, upload_world, start_gazelle, dot_torrents_dir):
    """The first ajax.php?action=index call returns HTTP 500. The real
    tenacity retry in BaseGazelleApi._request (RetryableError, 5 attempts,
    wait_fixed(1)) re-issues the index call, authenticate succeeds on the
    second attempt, and the whole wizard flow completes with an upload.
    Costs ~1s — the one fixed retry wait; no other sleeps."""
    app = make_fake_gazelle(browse_results=[dict(SEARCH_RESULT)])
    index_calls = {"count": 0}

    @web.middleware
    async def flaky_index(request, handler):
        if request.query.get("action") == "index":
            index_calls["count"] += 1
            if index_calls["count"] == 1:
                return web.Response(status=500, text="temporary tracker hiccup")
        return await handler(request)

    app.middlewares.append(flaky_index)
    base_url = await start_gazelle(app)

    m = jm()
    path = str(album_dir)
    job = m.create_threaded(
        "upload", "Upload to RED", http_wizard_factory(base_url, dot_torrents_dir, path), lock_key=path
    )

    await drive(job, HAPPY_PATH_SCRIPT)
    await join_job(job)

    assert job.status == "done", job.error
    assert index_calls["count"] == 2  # 500 once, retried exactly once, then cached
    assert len(app[UPLOADS]) == 1
    assert app[UPLOADS][0]["fields"]["auth"] == "fake-authkey"
    assert f"Successfully uploaded {base_url}/torrents.php?torrentid=424242" in "\n".join(job.log_lines)
