"""Tests for the webui job manager and the upload-adjacent HTTP/websocket API.

Covers salmon.webui.jobs.JobManager directly (async unit tests on a local
instance) and the FastAPI routers (spectrals, convert, checks, jobs, ws)
through fastapi.testclient.TestClient against salmon.webui.app.create_app().

All external work (sox spectrals, transcoding, integrity checking, image
hosts) is stubbed at the router-module seam; no network, no binaries.
"""

import asyncio
import contextlib
import os
import pathlib

import asyncclick as click
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from salmon import cfg
from salmon.common.progress import report_progress
from salmon.uploader.frequency import SpectrumResult
from salmon.uploader.spectrals import get_spectrals_path
from salmon.webui.app import create_app
from salmon.webui.jobs import (
    MAX_FINISHED_JOBS,
    SUBSCRIBER_QUEUE_SIZE,
    Job,
    JobConflictError,
    JobManager,
    manager,
)


def task_of(job) -> asyncio.Task:
    assert job.task is not None
    return job.task

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_manager():
    """Isolate tests from the module-level singleton the routers use."""
    manager.jobs.clear()
    manager._subscribers.clear()
    manager._active_lock_keys.clear()
    yield
    manager.jobs.clear()
    manager._subscribers.clear()
    manager._active_lock_keys.clear()


@pytest.fixture
def client():
    # base_url localhost passes TrustedHostMiddleware; the context manager
    # keeps one portal event loop alive so background job tasks survive
    # across requests.
    with TestClient(create_app(), base_url="http://localhost") as c:
        yield c


def join_job(client: TestClient, job_id: str) -> dict:
    """Wait for a job (loop task or worker thread) to finish; return its dict."""
    job = manager.jobs[job_id]
    if job.thread is not None:
        job.thread.join(timeout=15)
        assert not job.thread.is_alive(), f"job {job_id} thread did not finish"
    else:

        async def _wait() -> None:
            assert job.task is not None
            with contextlib.suppress(asyncio.CancelledError):
                await task_of(job)

        assert client.portal is not None
        client.portal.call(_wait)
    resp = client.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    return resp.json()


async def wait_running(job: Job) -> None:
    """Yield to the loop (no timed sleeps) until the job reports running."""
    for _ in range(1000):
        if job.status == "running":
            return
        await asyncio.sleep(0)
    raise AssertionError(f"job never reached running, status={job.status}")


@pytest.fixture
def specs_requests() -> list[str | None]:
    """Records the destination the router asks create_specs_folder to build."""
    return []


@pytest.fixture
def spectral_stubs(monkeypatch, tmp_path, specs_requests):
    """Stub audio probing and spectral generation; returns the specs dir."""
    specs_dir = tmp_path / "specs-out"

    def fake_gather_audio_info(path, sort_by_tracknumber=False):
        return {"01. Intro.flac": {"duration": 60}}

    def fake_create_specs_folder(path, spectrals_path=None):
        specs_requests.append(spectrals_path)
        specs_dir.mkdir(exist_ok=True)
        return str(specs_dir)

    async def fake_generate_spectrals_all(path, spectrals_path, audio_info):
        (specs_dir / "01-intro.png").write_bytes(b"PNG1")
        (specs_dir / "02-mittelteil.png").write_bytes(b"PNG2")
        (specs_dir / "notes.txt").write_text("not an image")
        return {1: "01-intro.png", 2: "02-mittelteil.png"}

    async def fake_generate_frequency_plots(path, files, out_dir):
        (specs_dir / "01 Frequency.png").write_bytes(b"PNGF")
        return [
            SpectrumResult(
                file="01. Intro.flac", image="01 Frequency.png", sample_rate=44100, cutoff_hz=21800.0, windows=9
            )
        ]

    monkeypatch.setattr("salmon.webui.routers.spectrals.gather_audio_info", fake_gather_audio_info)
    monkeypatch.setattr("salmon.webui.routers.spectrals.create_specs_folder", fake_create_specs_folder)
    monkeypatch.setattr("salmon.webui.routers.spectrals.generate_spectrals_all", fake_generate_spectrals_all)
    monkeypatch.setattr("salmon.webui.routers.spectrals.generate_frequency_plots", fake_generate_frequency_plots)
    return specs_dir


@pytest.fixture
def hanging_spectral_stubs(monkeypatch, tmp_path):
    """Spectral generation that blocks forever (until the job is cancelled)."""
    specs_dir = tmp_path / "specs-hang"

    def fake_gather_audio_info(path, sort_by_tracknumber=False):
        return {}

    def fake_create_specs_folder(path, spectrals_path=None):
        specs_dir.mkdir(exist_ok=True)
        return str(specs_dir)

    async def hang_forever(path, spectrals_path, audio_info):
        await asyncio.Event().wait()

    monkeypatch.setattr("salmon.webui.routers.spectrals.gather_audio_info", fake_gather_audio_info)
    monkeypatch.setattr("salmon.webui.routers.spectrals.create_specs_folder", fake_create_specs_folder)
    monkeypatch.setattr("salmon.webui.routers.spectrals.generate_spectrals_all", hang_forever)
    return specs_dir


@pytest.fixture
def integrity_stub(monkeypatch):
    async def fake_check_integrity(path):
        return True, "\x1b[32mok\x1b[0m"

    monkeypatch.setattr("salmon.checks.album.check_integrity", fake_check_integrity)


def run_generate_to_done(client: TestClient, album_dir) -> str:
    """POST a spectrals/generate job (stubbed) and wait for it; returns id."""
    resp = client.post("/api/spectrals/generate", json={"path": str(album_dir)})
    assert resp.status_code == 200
    job_id = resp.json()["id"]
    data = join_job(client, job_id)
    assert data["status"] == "done"
    return job_id


# ---------------------------------------------------------------------------
# JobManager unit tests
# ---------------------------------------------------------------------------


async def test_job_success_sets_done_result_and_finished_at():
    m = JobManager()

    async def factory(job):
        return {"answer": 42}

    job = m.create("demo", "Demo", factory, params={"p": 1})
    assert job.status == "queued"
    assert job.finished_at is None
    await task_of(job)
    assert job.status == "done"
    assert job.result == {"answer": 42}
    assert job.error is None
    assert job.finished_at is not None
    assert job.to_dict()["params"] == {"p": 1}


async def test_factory_value_error_sets_error_status_and_message():
    m = JobManager()

    async def factory(job):
        raise ValueError("boom")

    job = m.create("demo", "Demo", factory)
    await task_of(job)
    assert job.status == "error"
    assert job.error == "ValueError: boom"
    assert job.result is None


async def test_factory_click_abort_sets_friendly_error():
    m = JobManager()

    async def factory(job):
        raise click.Abort()

    job = m.create("demo", "Demo", factory)
    await task_of(job)
    assert job.status == "error"
    assert job.error == "Aborted."


async def test_factory_exception_group_single_leaf_is_unwrapped():
    m = JobManager()

    async def factory(job):
        raise ExceptionGroup("wrapper", [ValueError("boom")])

    job = m.create("demo", "Demo", factory)
    await task_of(job)
    assert job.status == "error"
    assert job.error == "ValueError: boom"


async def test_factory_exception_group_multiple_leaves_are_joined():
    m = JobManager()

    async def factory(job):
        raise ExceptionGroup("wrapper", [ValueError("a"), TypeError("b")])

    job = m.create("demo", "Demo", factory)
    await task_of(job)
    assert job.status == "error"
    assert job.error == "ValueError: a; TypeError: b"


async def test_factory_nested_exception_group_with_abort_prefers_abort_message():
    m = JobManager()

    async def factory(job):
        inner = ExceptionGroup("inner", [click.Abort()])
        raise ExceptionGroup("outer", [inner, ValueError("ignored")])

    job = m.create("demo", "Demo", factory)
    await task_of(job)
    assert job.status == "error"
    assert job.error == "Aborted."


async def test_cancel_running_job_sets_cancelled_and_releases_lock():
    m = JobManager()

    async def factory(job):
        await asyncio.Event().wait()

    job = m.create("demo", "Demo", factory, lock_key="/some/album")
    await wait_running(job)
    assert m.cancel(job.id) is True
    with contextlib.suppress(asyncio.CancelledError):
        await task_of(job)
    assert job.status == "cancelled"
    assert job.finished_at is not None
    assert "/some/album" not in m._active_lock_keys


async def test_cancel_unknown_or_finished_job_returns_false():
    m = JobManager()
    assert m.cancel("job-does-not-exist") is False

    async def factory(job):
        return "ok"

    job = m.create("demo", "Demo", factory)
    await task_of(job)
    assert m.cancel(job.id) is False


async def test_lock_key_conflict_raises_and_conflicting_job_is_not_registered():
    m = JobManager()
    release = asyncio.Event()

    async def factory(job):
        await release.wait()

    first = m.create("demo", "Demo", factory, lock_key="/album")
    with pytest.raises(JobConflictError, match="already running on: /album"):
        m.create("demo", "Demo 2", factory, lock_key="/album")
    assert len(m.jobs) == 1
    release.set()
    await task_of(first)


async def test_lock_released_after_finish_allows_second_job():
    m = JobManager()

    async def factory(job):
        return "ok"

    first = m.create("demo", "Demo", factory, lock_key="/album")
    await task_of(first)
    assert "/album" not in m._active_lock_keys
    second = m.create("demo", "Demo again", factory, lock_key="/album")
    await task_of(second)
    assert second.status == "done"


async def test_cancel_before_first_run_finalizes_job_and_releases_lock():
    # Cancelling a task before its coroutine ever ran skips _run's try/finally;
    # the done callback must finalize the job and release the lock instead.
    m = JobManager()

    async def factory(job):
        return "never runs"

    job = m.create("demo", "Demo", factory, lock_key="/leaky")
    assert m.cancel(job.id) is True  # cancelled before the loop ran the task
    with pytest.raises(asyncio.CancelledError):
        await task_of(job)
    assert job.status == "cancelled"
    assert job.finished_at is not None
    assert "/leaky" not in m._active_lock_keys
    second = m.create("demo", "Demo 2", factory, lock_key="/leaky")
    await task_of(second)
    assert second.status == "done"


async def test_subscriber_receives_created_status_progress_finished_in_order():
    m = JobManager()
    queue = m.subscribe()
    assert queue is not None

    async def factory(job):
        report_progress(1, 3, "first file")
        return "ok"

    job = m.create("demo", "Demo", factory)
    await task_of(job)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert [e["event"] for e in events] == ["created", "status", "progress", "finished"]

    created, status, progress, finished = events
    assert created["job"]["id"] == job.id
    assert created["job"]["status"] == "queued"
    assert status == {"event": "status", "job_id": job.id, "status": "running"}
    expected_progress = {"done": 1, "total": 3, "desc": "first file"}
    assert progress == {"event": "progress", "job_id": job.id, "progress": expected_progress}
    assert job.progress == expected_progress
    assert finished["job"]["status"] == "done"
    assert finished["job"]["result"] == "ok"


async def test_unsubscribed_queue_receives_no_events():
    m = JobManager()
    queue = m.subscribe()
    assert queue is not None
    m.unsubscribe(queue)

    async def factory(job):
        return "ok"

    job = m.create("demo", "Demo", factory)
    await task_of(job)
    assert queue.empty()


async def test_full_subscriber_queue_drops_oldest_event():
    m = JobManager()
    queue = m.subscribe()
    assert queue is not None
    for i in range(SUBSCRIBER_QUEUE_SIZE + 1):
        m._broadcast({"event": "tick", "i": i})

    assert queue.qsize() == SUBSCRIBER_QUEUE_SIZE
    first = queue.get_nowait()
    assert first["i"] == 1  # event 0 was dropped, newest event kept
    last = first
    while not queue.empty():
        last = queue.get_nowait()
    assert last["i"] == SUBSCRIBER_QUEUE_SIZE


async def test_prune_finished_evicts_oldest_beyond_max_finished_jobs():
    m = JobManager()

    async def factory(job):
        return None

    finished_jobs = []
    for _ in range(MAX_FINISHED_JOBS + 1):
        job = m.create("demo", "Demo", factory)
        await task_of(job)
        finished_jobs.append(job)

    # The next create prunes exactly the oldest finished job.
    extra = m.create("demo", "One more", factory)
    assert finished_jobs[0].id not in m.jobs
    assert finished_jobs[1].id in m.jobs
    assert len(m.jobs) == MAX_FINISHED_JOBS + 1
    await task_of(extra)


# ---------------------------------------------------------------------------
# TrustedHostMiddleware behavior
# ---------------------------------------------------------------------------


def test_default_loopback_bind_rejects_testserver_host_header():
    c = TestClient(create_app())  # base_url http://testserver
    resp = c.get("/api/jobs")
    assert resp.status_code == 400
    assert resp.text == "Invalid host header"


def test_loopback_bind_rejects_evil_host_header(client):
    resp = client.get("/api/jobs", headers={"host": "evil.example.com"})
    assert resp.status_code == 400
    assert resp.text == "Invalid host header"


def test_non_loopback_bind_skips_trusted_host_check():
    c = TestClient(create_app(host="0.0.0.0"))
    resp = c.get("/api/jobs")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Jobs endpoints
# ---------------------------------------------------------------------------


def test_jobs_list_initially_empty(client):
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_unknown_job_returns_404(client):
    resp = client.get("/api/jobs/job-does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Unknown job."


def test_cancel_unknown_job_returns_409(client):
    resp = client.post("/api/jobs/job-does-not-exist/cancel")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Job not found or already finished."


def test_cancel_finished_job_returns_409(client, album_dir, integrity_stub):
    resp = client.post("/api/checks/run", json={"path": str(album_dir), "checks": ["integrity"]})
    assert resp.status_code == 200
    job_id = resp.json()["id"]
    assert join_job(client, job_id)["status"] == "done"

    resp = client.post(f"/api/jobs/{job_id}/cancel")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Job not found or already finished."


def test_jobs_list_returns_newest_first(client, album_dir, integrity_stub):
    body = {"path": str(album_dir), "checks": ["integrity"]}
    first_id = client.post("/api/checks/run", json=body).json()["id"]
    second_id = client.post("/api/checks/run", json=body).json()["id"]
    join_job(client, first_id)
    join_job(client, second_id)

    listed = client.get("/api/jobs").json()
    assert [j["id"] for j in listed] == [second_id, first_id]


def test_job_factory_error_is_surfaced_in_job_dict(client, album_dir, monkeypatch):
    async def broken_check_integrity(path):
        raise ValueError("integrity exploded")

    monkeypatch.setattr("salmon.checks.album.check_integrity", broken_check_integrity)
    resp = client.post("/api/checks/run", json={"path": str(album_dir), "checks": ["integrity"]})
    assert resp.status_code == 200
    data = join_job(client, resp.json()["id"])
    assert data["status"] == "error"
    assert data["error"] == "ValueError: integrity exploded"
    assert data["result"] is None


# ---------------------------------------------------------------------------
# Spectrals endpoints
# ---------------------------------------------------------------------------


def test_spectrals_generate_nonexistent_path_returns_404(client):
    resp = client.post("/api/spectrals/generate", json={"path": "/definitely/not/here"})
    assert resp.status_code == 403
    assert "outside the configured salmon directories" in resp.json()["detail"]


def test_spectrals_generate_missing_body_field_returns_422(client):
    resp = client.post("/api/spectrals/generate", json={})
    assert resp.status_code == 422


@pytest.fixture
def library_album(tmp_path, monkeypatch):
    """An album inside a read-only library source."""
    lib = pathlib.Path(os.path.realpath(tmp_path)) / "library"
    album = lib / "Testartist - Testalbum (2024) [FLAC]"
    album.mkdir(parents=True)
    (album / "01. Intro.flac").write_bytes(b"fLaC" + bytes(2000))
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])
    return album


def test_spectrals_generate_allowed_for_a_library_album(client, library_album, spectral_stubs, specs_requests):
    # The images go to tmp_dir, so the album being read-only is irrelevant.
    resp = client.post("/api/spectrals/generate", json={"path": str(library_album)})
    assert resp.status_code == 200
    assert join_job(client, resp.json()["id"])["status"] == "done"
    # The vetted destination is the one handed to create_specs_folder, and it is outside the library.
    assert specs_requests == [get_spectrals_path(str(library_album))]
    assert not cfg.directory.is_library_path(specs_requests[0])


def test_spectrals_generate_refused_when_it_would_write_into_the_library(client, library_album, monkeypatch):
    # No tmp_dir means get_spectrals_path falls back to <album>/Spectrals.
    monkeypatch.setattr(cfg.directory, "tmp_dir", "")
    resp = client.post("/api/spectrals/generate", json={"path": str(library_album)})
    assert resp.status_code == 403
    assert "read-only library directory" in resp.json()["detail"]
    assert not (library_album / "Spectrals").exists()


def test_spectrals_generate_happy_path(client, album_dir, spectral_stubs):
    resp = client.post("/api/spectrals/generate", json={"path": str(album_dir)})
    assert resp.status_code == 200
    job = resp.json()
    assert job["type"] == "spectrals"
    assert job["title"] == f"Spectrals: {album_dir.name}"
    assert job["params"] == {"path": str(album_dir)}

    data = join_job(client, job["id"])
    assert data["status"] == "done"
    assert data["result"]["album_path"] == str(album_dir)
    assert data["result"]["spectrals_path"] == str(spectral_stubs)
    # keys are stringified for JSON, non-png files are excluded, list sorted
    assert data["result"]["spectral_ids"] == {"1": "01-intro.png", "2": "02-mittelteil.png"}
    # Frequency plots are shown but never posted, so they stay out of "files".
    assert data["result"]["files"] == ["01-intro.png", "02-mittelteil.png"]
    assert data["result"]["frequency"][0]["image"] == "01 Frequency.png"


def test_only_the_spectrograms_are_sent_to_the_image_host(client, album_dir, spectral_stubs, monkeypatch):
    uploaded: list[str] = []

    async def fake_upload_images(files, host):
        uploaded.extend(files)
        return ["https://host/x.png" for _ in files]

    monkeypatch.setattr("salmon.webui.routers.spectrals.upload_images", fake_upload_images)
    job_id = client.post("/api/spectrals/generate", json={"path": str(album_dir)}).json()["id"]
    join_job(client, job_id)

    upload = client.post("/api/spectrals/upload", json={"job_id": job_id, "host": "catbox"})
    join_job(client, upload.json()["id"])

    assert [os.path.basename(f) for f in uploaded] == ["01-intro.png", "02-mittelteil.png"]


def test_a_frequency_plot_is_still_served_even_though_it_is_not_uploaded(client, album_dir, spectral_stubs):
    job_id = client.post("/api/spectrals/generate", json={"path": str(album_dir)}).json()["id"]
    join_job(client, job_id)

    resp = client.get(f"/api/spectrals/{job_id}/image/01%20Frequency.png")

    assert resp.status_code == 200
    assert resp.content == b"PNGF"


def test_spectrals_generate_second_post_on_same_path_conflicts(client, album_dir, hanging_spectral_stubs):
    first = client.post("/api/spectrals/generate", json={"path": str(album_dir)})
    assert first.status_code == 200
    job_id = first.json()["id"]

    second = client.post("/api/spectrals/generate", json={"path": str(album_dir)})
    assert second.status_code == 409
    assert second.json()["detail"] == f"A job is already running on: {album_dir}"

    # Cancelling the running job releases the path lock ...
    cancelled = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"cancelled": job_id}
    assert join_job(client, job_id)["status"] == "cancelled"

    # ... so a third POST succeeds with a fresh job.
    third = client.post("/api/spectrals/generate", json={"path": str(album_dir)})
    assert third.status_code == 200
    new_id = third.json()["id"]
    assert new_id != job_id
    client.post(f"/api/jobs/{new_id}/cancel")
    join_job(client, new_id)


def test_spectrals_endpoints_on_unfinished_job_return_404(client, album_dir, hanging_spectral_stubs):
    job_id = client.post("/api/spectrals/generate", json={"path": str(album_dir)}).json()["id"]

    image = client.get(f"/api/spectrals/{job_id}/image/whatever.png")
    assert image.status_code == 404
    upload = client.post("/api/spectrals/upload", json={"job_id": job_id, "host": "catbox"})
    assert upload.status_code == 404

    client.post(f"/api/jobs/{job_id}/cancel")
    join_job(client, job_id)


def test_spectrals_image_unknown_job_returns_404(client):
    resp = client.get("/api/spectrals/job-does-not-exist/image/x.png")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "No finished spectrals job with this id."


def test_spectrals_image_unknown_filename_returns_404(client, album_dir, spectral_stubs):
    job_id = run_generate_to_done(client, album_dir)
    resp = client.get(f"/api/spectrals/{job_id}/image/not-there.png")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Unknown spectral image."


def test_spectrals_image_path_traversal_attempt_returns_404(client, album_dir, spectral_stubs, tmp_path):
    (tmp_path / "secret.png").write_bytes(b"TOP SECRET")
    job_id = run_generate_to_done(client, album_dir)
    resp = client.get(f"/api/spectrals/{job_id}/image/..%2Fsecret.png")
    assert resp.status_code == 404


def test_spectrals_image_serves_png_bytes(client, album_dir, spectral_stubs):
    job_id = run_generate_to_done(client, album_dir)
    resp = client.get(f"/api/spectrals/{job_id}/image/01-intro.png")
    assert resp.status_code == 200
    assert resp.content == b"PNG1"
    assert resp.headers["content-type"] == "image/png"


def test_spectrals_upload_unknown_job_returns_404(client):
    resp = client.post("/api/spectrals/upload", json={"job_id": "job-does-not-exist"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "No finished spectrals job with this id."


def test_spectrals_upload_unknown_host_returns_422(client, album_dir, spectral_stubs):
    job_id = run_generate_to_done(client, album_dir)
    resp = client.post("/api/spectrals/upload", json={"job_id": job_id, "host": "definitely-not"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Unknown image host: definitely-not"


def test_spectrals_upload_happy_path_with_explicit_host(client, album_dir, spectral_stubs, monkeypatch):
    uploaded = []

    async def fake_upload_images(filepaths, image_host):
        uploaded.extend(filepaths)
        return [f"https://img.example/{i}" for i, _ in enumerate(filepaths)]

    monkeypatch.setattr("salmon.webui.routers.spectrals.upload_images", fake_upload_images)
    gen_id = run_generate_to_done(client, album_dir)

    resp = client.post("/api/spectrals/upload", json={"job_id": gen_id, "host": "catbox"})
    assert resp.status_code == 200
    job = resp.json()
    assert job["type"] == "spectrals-upload"
    assert job["title"] == f"Upload spectrals: {album_dir.name}"
    assert job["params"] == {"job_id": gen_id, "host": "catbox"}

    data = join_job(client, job["id"])
    assert data["status"] == "done"
    assert data["result"] == {"host": "catbox", "urls": ["https://img.example/0", "https://img.example/1"]}
    assert uploaded == [str(spectral_stubs / "01-intro.png"), str(spectral_stubs / "02-mittelteil.png")]

    # An upload job's id is not valid for the image endpoint (type guard).
    resp = client.get(f"/api/spectrals/{job['id']}/image/01-intro.png")
    assert resp.status_code == 404


def test_spectrals_upload_falls_back_to_configured_host(client, album_dir, spectral_stubs, monkeypatch):
    async def fake_upload_images(filepaths, image_host):
        return ["https://img.example/a"]

    monkeypatch.setattr("salmon.webui.routers.spectrals.upload_images", fake_upload_images)
    monkeypatch.setattr(cfg.image, "specs_uploader", "imgbox")
    gen_id = run_generate_to_done(client, album_dir)

    resp = client.post("/api/spectrals/upload", json={"job_id": gen_id})
    assert resp.status_code == 200
    assert resp.json()["params"]["host"] == "imgbox"
    data = join_job(client, resp.json()["id"])
    assert data["result"]["host"] == "imgbox"


# ---------------------------------------------------------------------------
# Convert endpoints
# ---------------------------------------------------------------------------


def test_transcode_invalid_bitrate_returns_422(client, album_dir):
    resp = client.post("/api/convert/transcode", json={"path": str(album_dir), "bitrate": "192"})
    assert resp.status_code == 422


def test_transcode_nonexistent_dir_returns_404(client):
    resp = client.post("/api/convert/transcode", json={"path": "/definitely/not/here", "bitrate": "V0"})
    assert resp.status_code == 403
    assert "outside the configured salmon directories" in resp.json()["detail"]


def test_downconvert_nonexistent_dir_returns_404(client):
    resp = client.post("/api/convert/downconvert", json={"path": "/definitely/not/here"})
    assert resp.status_code == 403


def test_transcode_happy_path(client, album_dir, monkeypatch):
    calls = []

    async def fake_transcode_folder(path, bitrate):
        calls.append((path, bitrate))
        return f"{path} [MP3 {bitrate}]"

    monkeypatch.setattr("salmon.webui.routers.convert.transcode_folder", fake_transcode_folder)
    resp = client.post("/api/convert/transcode", json={"path": str(album_dir), "bitrate": "V0"})
    assert resp.status_code == 200
    job = resp.json()
    assert job["type"] == "transcode"
    assert job["title"] == f"Transcode V0: {album_dir.name}"
    assert job["params"] == {"path": str(album_dir), "bitrate": "V0"}

    data = join_job(client, job["id"])
    assert data["status"] == "done"
    assert data["result"] == {"output_path": f"{album_dir} [MP3 V0]"}
    assert calls == [(str(album_dir), "V0")]


def test_transcode_and_downconvert_share_the_path_lock(client, album_dir, monkeypatch):
    async def hang_transcode(path, bitrate):
        await asyncio.Event().wait()

    async def fake_convert_folder(path):
        return 44100, f"{path} [16-44]"

    monkeypatch.setattr("salmon.webui.routers.convert.transcode_folder", hang_transcode)
    monkeypatch.setattr("salmon.webui.routers.convert.convert_folder", fake_convert_folder)

    first = client.post("/api/convert/transcode", json={"path": str(album_dir), "bitrate": "320"})
    assert first.status_code == 200
    job_id = first.json()["id"]

    # Both convert endpoints lock on the album path.
    conflict = client.post("/api/convert/downconvert", json={"path": str(album_dir)})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == f"A job is already running on: {album_dir}"
    conflict = client.post("/api/convert/transcode", json={"path": str(album_dir), "bitrate": "V0"})
    assert conflict.status_code == 409

    client.post(f"/api/jobs/{job_id}/cancel")
    assert join_job(client, job_id)["status"] == "cancelled"

    after = client.post("/api/convert/downconvert", json={"path": str(album_dir)})
    assert after.status_code == 200
    data = join_job(client, after.json()["id"])
    assert data["status"] == "done"
    assert data["result"] == {"output_path": f"{album_dir} [16-44]"}


# ---------------------------------------------------------------------------
# Checks endpoint
# ---------------------------------------------------------------------------


def test_checks_run_invalid_check_name_returns_422(client, album_dir):
    resp = client.post("/api/checks/run", json={"path": str(album_dir), "checks": ["log", "bogus"]})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Invalid checks: ['bogus']"


def test_checks_run_no_checks_still_verifies_the_source(client, album_dir):
    """Skipping every file check is legitimate — source and duplicates still gate."""
    resp = client.post("/api/checks/run", json={"path": str(album_dir), "checks": []})
    assert resp.status_code == 200
    data = join_job(client, resp.json()["id"])
    assert data["status"] == "done"
    verdicts = {r["id"]: r["verdict"] for r in data["result"]["rows"]}
    assert verdicts["integrity"] == "skip"
    assert "source" in verdicts


def test_checks_run_nonexistent_dir_returns_404_before_check_validation(client):
    resp = client.post("/api/checks/run", json={"path": "/definitely/not/here", "checks": ["bogus"]})
    assert resp.status_code == 403


def test_checks_run_integrity_and_log_happy_path(client, album_dir, integrity_stub):
    # An empty log makes cambia raise (per-log error branch); garbage text is
    # parsed "successfully" with a negative score.
    (album_dir / "empty.log").write_text("")
    (album_dir / "garbage.log").write_text("this is not an EAC log")

    resp = client.post("/api/checks/run", json={"path": str(album_dir), "checks": ["integrity", "log"]})
    assert resp.status_code == 200
    job = resp.json()
    assert job["type"] == "checks"
    assert job["title"] == f"Checks (integrity, log): {album_dir.name}"

    data = join_job(client, job["id"])
    assert data["status"] == "done"
    # ANSI styling from the integrity checker is stripped for the API
    assert data["result"]["raw"]["integrity"] == {"passed": True, "details": "ok"}
    verdicts = {r["id"]: r["verdict"] for r in data["result"]["rows"]}
    assert verdicts["integrity"] == "ok"
    logs = {entry["file"]: entry for entry in data["result"]["raw"]["log"]["logs"]}
    assert set(logs) == {"empty.log", "garbage.log"}
    assert "Empty request body" in logs["empty.log"]["error"]
    assert logs["garbage.log"]["score"] == -15
    # cambia's Integrity enum stringifies as "<Integrity.Unknown>"; the router
    # must strip both the class prefix and the trailing ">".
    assert logs["garbage.log"]["checksum_integrity"] == "Unknown"


# ---------------------------------------------------------------------------
# Websocket /api/ws
# ---------------------------------------------------------------------------


def _wait_until_subscribed(client: TestClient) -> None:
    """Spin the portal loop until the ws endpoint registered its queue."""
    assert client.portal is not None
    for _ in range(500):
        if client.portal.call(lambda: bool(manager._subscribers)):
            return
    raise AssertionError("websocket endpoint never subscribed to the job manager")


def test_ws_localhost_origin_receives_job_events_in_order(client, album_dir, integrity_stub):
    headers = {"host": "localhost", "origin": "http://localhost:5173"}
    with client.websocket_connect("/api/ws", headers=headers) as ws:
        _wait_until_subscribed(client)
        resp = client.post("/api/checks/run", json={"path": str(album_dir), "checks": ["integrity"]})
        assert resp.status_code == 200
        job_id = resp.json()["id"]

        created = ws.receive_json()
        assert created["event"] == "created"
        assert created["job"]["id"] == job_id
        assert created["job"]["status"] == "queued"

        status = ws.receive_json()
        assert status == {"event": "status", "job_id": job_id, "status": "running"}

        finished = ws.receive_json()
        assert finished["event"] == "finished"
        assert finished["job"]["id"] == job_id
        assert finished["job"]["status"] == "done"
        assert finished["job"]["result"]["raw"]["integrity"]["passed"] is True


def test_ws_without_origin_header_is_accepted(client):
    # Non-browser clients (curl, scripts) send no Origin and are allowed.
    with client.websocket_connect("/api/ws", headers={"host": "localhost"}):
        _wait_until_subscribed(client)


def test_ws_cross_site_origin_is_rejected_with_1008(client):
    headers = {"host": "localhost", "origin": "https://evil.example"}
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/api/ws", headers=headers),
    ):
        pass  # pragma: no cover - connection is refused before entry
    assert exc_info.value.code == 1008


# ---------------------------------------------------------------------------
# Spectrals housekeeping
# ---------------------------------------------------------------------------


def _finished_spectrals_job(specs_dir) -> Job:
    job = Job("spectrals", "Spectrals: x", {})
    job.status = "done"
    job.result = {"album_path": "/x", "spectrals_path": str(specs_dir), "files": ["01 Full.png"], "frequency": []}
    return job


def _specs_under(root, name) -> pathlib.Path:
    folder = pathlib.Path(os.path.realpath(root)) / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01 Full.png").write_bytes(b"PNG")
    return folder


def test_discarding_removes_the_generated_images(tmp_path, monkeypatch):
    from salmon.webui.routers.spectrals import _discard_spectrals

    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_path))
    specs = _specs_under(tmp_path, "spectrals_x")
    _discard_spectrals(_finished_spectrals_job(specs))
    assert not specs.exists()


def test_discarding_refuses_a_path_outside_the_configured_directories(tmp_path):
    from salmon.webui.routers.spectrals import _discard_spectrals

    outside = _specs_under(tmp_path, "spectrals_elsewhere")
    _discard_spectrals(_finished_spectrals_job(outside))
    assert outside.exists(), "rmtree must not follow a result path that has left the roots"


def test_discard_endpoint_empties_the_job_result(client, tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_path))
    specs = _specs_under(tmp_path, "spectrals_ep")
    job = _finished_spectrals_job(specs)
    manager.jobs[job.id] = job

    resp = client.delete(f"/api/spectrals/{job.id}")

    assert resp.status_code == 200
    assert not specs.exists()
    assert manager.jobs[job.id].result["files"] == []
    assert manager.jobs[job.id].result["discarded"] is True


def test_discard_endpoint_404s_for_an_unknown_job(client):
    assert client.delete("/api/spectrals/job-does-not-exist").status_code == 404


def test_evicting_a_finished_job_runs_its_cleanup(monkeypatch, tmp_path):
    from salmon.webui import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_FINISHED_JOBS", 0)
    local = JobManager()
    job = _finished_spectrals_job(tmp_path)
    evicted: list[str] = []
    job.on_evict = lambda j: evicted.append(j.id)
    local.jobs[job.id] = job

    local._prune_finished()

    assert evicted == [job.id], "a job that left files behind must be told when it is dropped"
    assert job.id not in local.jobs


def test_a_failing_cleanup_does_not_stop_the_eviction(monkeypatch, tmp_path):
    from salmon.webui import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_FINISHED_JOBS", 0)
    local = JobManager()
    job = _finished_spectrals_job(tmp_path)

    def boom(_job):
        raise OSError("disk gone")

    job.on_evict = boom
    local.jobs[job.id] = job

    local._prune_finished()

    assert job.id not in local.jobs
