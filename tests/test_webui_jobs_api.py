"""Tests for the webui job manager and the upload-adjacent HTTP/websocket API.

Covers salmon.webui.jobs.JobManager directly (async unit tests on a local
instance) and the FastAPI routers (spectrals, convert, checks, jobs, ws)
through fastapi.testclient.TestClient against salmon.webui.app.create_app().

All external work (sox spectrals, transcoding, integrity checking, image
hosts) is stubbed at the router-module seam; no network, no binaries.
"""

import asyncio
import contextlib
import io
import os
import pathlib
import threading
import time
import unicodedata
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import asyncclick as click
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect
from test_trackers_account import FakeTracker, _clients, _post  # pyright: ignore[reportMissingImports]
from webui_client import reset_manager, web_client  # pyright: ignore[reportMissingImports]

from salmon import cfg, interaction
from salmon.checks.integrity import IntegrityResult
from salmon.common.progress import report_progress
from salmon.errors import UnknownOutcomeError
from salmon.trackers import account as tracker_account
from salmon.trackers import base as tracker_base
from salmon.uploader import record
from salmon.uploader.frequency import SpectrumResult
from salmon.uploader.spectrals import get_spectrals_path
from salmon.webui import egress
from salmon.webui import jobs as jobs_mod
from salmon.webui.jobs import (
    MAX_FINISHED_JOBS,
    SUBSCRIBER_QUEUE_SIZE,
    UNKNOWN_OUTCOME,
    Job,
    JobCapacityError,
    JobConflictError,
    JobManager,
    manager,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

# The bind address under test: nothing is served on it.
LAN_BIND = "0.0.0.0"  # nosec B104


def _png(shade: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (2, 2), shade).save(buffer, "PNG")
    return buffer.getvalue()


# Real images: nothing else may go to an image host.
PNG1, PNG2, PNGF = _png(1), _png(2), _png(3)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_manager():
    """Isolate tests from the module-level singleton the routers use."""
    reset_manager()
    yield
    reset_manager()


@pytest.fixture
def client():
    # localhost passes the Host check; the context manager keeps one portal loop alive for background jobs.
    with web_client() as c:
        yield c


def join_job(client: TestClient, job_id: str) -> dict:
    """Wait for a job to finish and its worker thread to end; return its dict."""
    job = manager.jobs[job_id]
    deadline = time.monotonic() + 15
    while job.finished_at is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert job.finished_at is not None, f"job {job_id} did not finish: {job.status}"
    if job.thread is not None:
        job.thread.join(timeout=15)
        assert not job.thread.is_alive(), f"job {job_id} thread did not finish"
    resp = client.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    return resp.json()


def wait_task(job_id: str) -> None:
    """Until the job's task runs on its thread's loop, so that a cancel reaches it."""
    deadline = time.monotonic() + 15
    while manager.jobs[job_id]._thread_task is None:
        assert time.monotonic() < deadline, f"job {job_id} never started its task"
        time.sleep(0.005)


async def until(condition: Callable[[], object], what: str) -> None:
    """Yield to the loop until `condition` holds."""
    deadline = time.monotonic() + 15
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def finished(job: Job) -> Job:
    """Yield to the loop until the job has finished and its worker thread ended."""
    await until(
        lambda: job.finished_at is not None and (job.thread is None or not job.thread.is_alive()),
        f"{job.id} to finish (status {job.status})",
    )
    return job


async def task_started(job: Job) -> None:
    """Until the job's task runs on its thread's loop, so that a cancel reaches it."""
    await until(lambda: job._thread_task is not None, f"{job.id} to start its task")


def gated() -> tuple[threading.Event, Callable[[Job], Coroutine[Any, Any, str]]]:
    """A job body that waits for the event, off its loop, then returns "through"."""
    gate = threading.Event()

    async def run(job: Job) -> str:
        await asyncio.to_thread(gate.wait, 15)
        return "through"

    return gate, run


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
        (specs_dir / "01-intro.png").write_bytes(PNG1)
        (specs_dir / "02-mittelteil.png").write_bytes(PNG2)
        (specs_dir / "notes.txt").write_text("not an image")
        return {1: "01. Intro.flac", 2: "02. Mittelteil.flac"}

    async def fake_generate_frequency_plots(path, files, plot_paths):
        plot = plot_paths["01. Intro.flac"]
        Path(plot).write_bytes(PNGF)
        return [
            SpectrumResult(
                file="01. Intro.flac", image=os.path.basename(plot), sample_rate=44100, cutoff_hz=21800.0, windows=9
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
        return IntegrityResult(True, "\x1b[32mok\x1b[0m")

    monkeypatch.setattr("salmon.checks.album_web.check_integrity", fake_check_integrity)


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

    job = m.create_threaded("demo", "Demo", factory, params={"p": 1})
    assert job.finished_at is None
    await finished(job)
    assert job.status == "done"
    assert job.result == {"answer": 42}
    assert job.error is None
    assert job.finished_at is not None
    assert job.to_dict()["params"] == {"p": 1}


async def test_factory_value_error_sets_error_status_and_message():
    m = JobManager()

    async def factory(job):
        raise ValueError("boom")

    job = await finished(m.create_threaded("demo", "Demo", factory))
    assert job.status == "failed"
    assert job.error == "ValueError: boom"
    assert job.result is None


async def test_factory_click_abort_sets_friendly_error():
    m = JobManager()

    async def factory(job):
        raise click.Abort()

    job = await finished(m.create_threaded("demo", "Demo", factory))
    assert job.status == "failed"
    assert job.error == "Aborted."


async def test_factory_exception_group_single_leaf_is_unwrapped():
    m = JobManager()

    async def factory(job):
        raise ExceptionGroup("wrapper", [ValueError("boom")])

    job = await finished(m.create_threaded("demo", "Demo", factory))
    assert job.status == "failed"
    assert job.error == "ValueError: boom"


async def test_factory_exception_group_multiple_leaves_are_joined():
    m = JobManager()

    async def factory(job):
        raise ExceptionGroup("wrapper", [ValueError("a"), TypeError("b")])

    job = await finished(m.create_threaded("demo", "Demo", factory))
    assert job.status == "failed"
    assert job.error == "ValueError: a; TypeError: b"


async def test_factory_nested_exception_group_with_abort_prefers_abort_message():
    m = JobManager()

    async def factory(job):
        inner = ExceptionGroup("inner", [click.Abort()])
        raise ExceptionGroup("outer", [inner, ValueError("ignored")])

    job = await finished(m.create_threaded("demo", "Demo", factory))
    assert job.status == "failed"
    assert job.error == "Aborted."


async def test_cancel_running_job_sets_cancelled_and_releases_lock():
    m = JobManager()

    async def factory(job):
        await asyncio.Event().wait()

    job = m.create_threaded("demo", "Demo", factory, lock_key="/some/album")
    await task_started(job)
    cancelled = m.cancel(job.id)
    await finished(job)
    assert cancelled is True
    assert job.status == "cancelled"
    assert job.finished_at is not None
    assert "/some/album" not in m._active_lock_keys


async def test_every_task_of_a_cancelled_job_sees_its_cancel_also_one_started_after_it():
    # So none of them sends a tracker anything that changes it (tracker_account.caller_cancelled).
    m = JobManager()
    seen: list[bool] = []

    async def look() -> None:
        seen.append(tracker_account.caller_cancelled())

    async def factory(job):
        # Goes on after the cancel, as a job whose cancel came back as an unknown outcome does.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.create_task(look())
            await asyncio.Event().wait()
        await asyncio.create_task(look())

    job = m.create_threaded("demo", "Demo", factory)
    await until(lambda: len(seen) == 1, "the job's first look")
    m.cancel(job.id)
    await finished(job)
    assert seen == [False, True]


async def test_cancel_unknown_or_finished_job_returns_false():
    m = JobManager()
    unknown = m.cancel("job-does-not-exist")
    assert unknown is False

    async def factory(job):
        return "ok"

    job = await finished(m.create_threaded("demo", "Demo", factory))
    again = m.cancel(job.id)
    assert again is False


async def test_lock_key_conflict_raises_and_conflicting_job_is_not_registered():
    m = JobManager()
    gate, run = gated()

    first = m.create_threaded("demo", "Demo", run, lock_key="/album")
    with pytest.raises(JobConflictError, match="already running on: /album"):
        m.create_threaded("demo", "Demo 2", run, lock_key="/album")
    assert len(m.jobs) == 1
    gate.set()
    await finished(first)


async def test_lock_released_after_finish_allows_second_job():
    m = JobManager()

    async def factory(job):
        return "ok"

    await finished(m.create_threaded("demo", "Demo", factory, lock_key="/album"))
    assert "/album" not in m._active_lock_keys
    second = await finished(m.create_threaded("demo", "Demo again", factory, lock_key="/album"))
    assert second.status == "done"


async def test_a_cancel_as_a_jobs_thread_makes_its_task_is_not_lost(monkeypatch):
    in_window, go = threading.Event(), threading.Event()

    class HeldFlag(threading.Event):
        def is_set(self) -> bool:
            seen = super().is_set()
            # The thread's first check: held there, between reading the flag and making its task.
            if not in_window.is_set():
                in_window.set()
                go.wait(15)
            return seen

    class HeldAsker(jobs_mod._JobAsker):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.cancel_requested = HeldFlag()

    class LingeringLock:
        """The job's lock, but its thread lingers once it lets go, so a cancel waiting on it reads first."""

        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __enter__(self) -> None:
            self._lock.acquire()

        def __exit__(self, *_: object) -> None:
            self._lock.release()
            if threading.current_thread() is not threading.main_thread():
                time.sleep(0.2)

    class LingeringJob(jobs_mod.Job):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._lock = LingeringLock()  # pyright: ignore[reportAttributeAccessIssue]

    monkeypatch.setattr(jobs_mod, "_JobAsker", HeldAsker)
    monkeypatch.setattr(jobs_mod, "Job", LingeringJob)
    ran = []

    async def body(job):
        ran.append("started")
        await asyncio.sleep(0)
        ran.append("went on")

    m = JobManager()
    job = m.create_threaded("demo", "Demo", body)
    await until(in_window.is_set, "the job's thread to be about to make its task")
    threading.Timer(0.2, go.set).start()
    cancelled = m.cancel(job.id)
    done = await finished(job)
    assert cancelled is True
    assert done.status == "cancelled"
    assert "went on" not in ran


async def test_cancelling_a_queued_job_ends_it_unrun_and_releases_its_lock():
    m = JobManager()
    gate, run = gated()
    ran = []

    async def never(job):
        ran.append(job.id)

    busy = [m.create_threaded("demo", f"Busy {n}", run) for n in range(m.max_jobs)]
    job = m.create_threaded("demo", "Demo", never, lock_key="/leaky")
    assert job.status == "queued"
    cancelled = m.cancel(job.id)
    assert cancelled is True
    assert job.status == "cancelled"
    assert job.finished_at is not None
    assert job.thread is None
    assert "/leaky" not in m._active_lock_keys
    again = m.cancel(job.id)
    assert again is False
    gate.set()
    for each in busy:
        await finished(each)
    assert ran == []


async def test_subscriber_receives_created_status_progress_finished_in_order():
    m = JobManager()
    queue = m.subscribe()
    assert queue is not None

    async def factory(job):
        report_progress(1, 3, "first file")
        return "ok"

    job = await finished(m.create_threaded("demo", "Demo", factory))

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert [e["event"] for e in events] == ["created", "status", "progress", "finished"]

    created, status, progress, finished_event = events
    assert created["job"]["id"] == job.id
    assert created["job"]["status"] == "queued"
    assert status == {"event": "status", "job_id": job.id, "status": "running"}
    expected_progress = {"done": 1, "total": 3, "desc": "first file"}
    assert progress == {"event": "progress", "job_id": job.id, "progress": expected_progress}
    assert job.progress == expected_progress
    assert finished_event["job"]["status"] == "done"
    assert finished_event["job"]["result"] == "ok"


async def test_unsubscribed_queue_receives_no_events():
    m = JobManager()
    queue = m.subscribe()
    assert queue is not None
    m.unsubscribe(queue)

    async def factory(job):
        return "ok"

    await finished(m.create_threaded("demo", "Demo", factory))
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

    finished_jobs = [await finished(m.create_threaded("demo", "Demo", factory)) for _ in range(MAX_FINISHED_JOBS + 1)]

    # The next create prunes exactly the oldest finished job.
    extra = m.create_threaded("demo", "One more", factory)
    assert finished_jobs[0].id not in m.jobs
    assert finished_jobs[1].id in m.jobs
    assert len(m.jobs) == MAX_FINISHED_JOBS + 1
    await finished(extra)


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


async def test_web_max_jobs_says_how_many_run_at_once(monkeypatch):
    monkeypatch.setattr(cfg.web, "max_jobs", 1)
    m = JobManager()
    gate, run = gated()
    first, second = (m.create_threaded("demo", name, run) for name in "ab")

    assert [first.status, second.status] == ["running", "queued"]
    gate.set()
    await finished(first)
    await finished(second)


async def test_at_most_max_jobs_run_and_the_rest_wait_in_order():
    m = JobManager(max_jobs=2)
    gates = {name: gated() for name in "abcd"}
    a, b, c, d = (m.create_threaded("demo", name, gates[name][1]) for name in "abcd")

    assert [job.status for job in (a, b, c, d)] == ["running", "running", "queued", "queued"]
    gates["a"][0].set()
    await finished(a)
    await until(lambda: c.status == "running", "c to start")
    assert d.status == "queued"
    for gate, _run in gates.values():
        gate.set()
    for job in (b, c, d):
        await finished(job)
    assert [job.result for job in (a, b, c, d)] == ["through"] * 4


async def test_a_second_job_on_the_same_folder_waits_and_another_folder_goes_on(tmp_path):
    album = tmp_path / "album"
    album.mkdir()
    # The same folder by another path.
    (tmp_path / "link").symlink_to(album)
    other = tmp_path / "other"
    other.mkdir()
    m = JobManager()
    gate, run = gated()

    first = m.create_threaded("demo", "First", run, folder=str(album))
    same = m.create_threaded("demo", "Same", run, folder=str(tmp_path / "link"))
    elsewhere = m.create_threaded("demo", "Elsewhere", run, folder=str(other))

    assert (first.status, same.status, elsewhere.status) == ("running", "queued", "running")
    gate.set()
    for each in (first, same, elsewhere):
        done = await finished(each)
        assert done.status == "done"


@pytest.mark.parametrize(
    "swap", ["a link elsewhere", "another folder", "a parent leading elsewhere", "moved away and linked back", "gone"]
)
async def test_a_job_whose_folder_was_swapped_while_it_waited_does_not_start(tmp_path, swap):
    parent = tmp_path / "downloads"
    album = parent / "album"
    album.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "album").mkdir(parents=True)
    gate, run = gated()
    ran = []

    async def body(job):
        ran.append(job.id)

    m = JobManager()
    first = m.create_threaded("demo", "First", run, folder=str(album))
    waiting = m.create_threaded("demo", "Waiting", body, folder=str(album))
    assert waiting.status == "queued"
    if swap == "a link elsewhere":
        album.rename(parent / "album-old")
        album.symlink_to(outside / "album")
    elif swap == "another folder":
        album.rename(parent / "album-old")
        album.mkdir()
    elif swap == "a parent leading elsewhere":
        parent.rename(tmp_path / "downloads-old")
        parent.symlink_to(outside)
    elif swap == "moved away and linked back":
        album.rename(outside / "moved")
        album.symlink_to(outside / "moved")
    else:
        album.rename(parent / "album-old")
    gate.set()
    await finished(first)
    done = await finished(waiting)

    assert (done.status, ran) == ("failed", [])
    assert done.error == f"FolderChangedError: {album} changed or moved while the job waited: start it again."


async def test_the_queue_is_bounded(monkeypatch):
    monkeypatch.setattr(jobs_mod, "MAX_QUEUED_JOBS", 2)
    m = JobManager()
    gate, run = gated()

    started = [m.create_threaded("demo", str(n), run) for n in range(m.max_jobs + 2)]
    with pytest.raises(JobCapacityError, match="2 jobs are already waiting"):
        m.create_threaded("demo", "One too many", run)
    assert len(m.jobs) == m.max_jobs + 2
    gate.set()
    for each in started:
        await finished(each)


async def test_a_cancelled_jobs_worker_still_in_its_folder_holds_back_the_next_job(tmp_path):
    album = tmp_path / "album"
    album.mkdir()
    in_folder, release = threading.Event(), threading.Event()
    overlap: list[bool] = []

    def write_tags() -> None:
        # A to_thread worker: cancelling the task cannot stop it.
        in_folder.set()
        release.wait(15)

    async def first_body(job):
        await asyncio.to_thread(write_tags)

    async def second_body(job):
        overlap.append(not release.is_set())
        return "ran"

    m = JobManager()
    first = m.create_threaded("tag", "First", first_body, folder=str(album))
    second = m.create_threaded("upload", "Second", second_body, folder=str(album))
    await task_started(first)
    await until(in_folder.is_set, "the worker to be in the folder")
    cancelled = m.cancel(first.id)
    assert cancelled
    await asyncio.sleep(0.3)
    # Still running while its worker holds the folder: neither finished, nor cancelled, nor prunable yet.
    held = (first.finished_at, first.status, second.status)
    release.set()
    await finished(first)
    await finished(second)

    assert held == (None, "running", "queued")
    assert (first.status, second.status, overlap) == ("cancelled", "done", [False])


async def test_a_worker_a_finished_job_left_cannot_ask_a_question():
    asked: list[str] = []

    def late_question() -> None:
        time.sleep(0.1)
        try:
            asyncio.run(interaction.confirm("Still there?"))
        except BaseException as e:  # noqa: BLE001 - what the question raised is the result
            asked.append(type(e).__name__)

    left: list[asyncio.Task] = []

    async def leaves_a_worker(job):
        # to_thread, as jobs start their workers: it carries the job's context, and so its questions.
        left.append(asyncio.create_task(asyncio.to_thread(late_question)))
        return "left"

    job = await finished(JobManager().create_threaded("demo", "Demo", leaves_a_worker))

    assert (job.status, job.question, asked) == ("done", None, ["CancelledError"])


async def test_a_thread_that_fails_to_start_hands_its_slot_to_the_next_job():
    m = JobManager()
    gates = {name: gated() for name in "abcd"}
    a, b, c, d = (m.create_threaded("demo", name, gates[name][1]) for name in "abcd")

    def no_thread() -> None:
        raise RuntimeError("can't start new thread")

    m._queue[c].start = no_thread
    gates["a"][0].set()
    await finished(a)
    await until(lambda: d.status == "running", "d to take the free slot")
    for gate, _run in gates.values():
        gate.set()
    for each in (b, d):
        await finished(each)

    assert (c.status, c.error, c.finished_at is not None) == ("failed", "Failed to start the worker thread.", True)
    assert c.asker is not None and c.asker.closed.is_set()
    assert d.result == "through"


async def test_the_same_folder_by_another_case_or_unicode_form_waits(tmp_path):
    folder = tmp_path / unicodedata.normalize("NFC", "Café Album")
    folder.mkdir()
    spellings = [str(tmp_path / "café album"), str(tmp_path / unicodedata.normalize("NFD", "Café Album"))]
    others = [each for each in spellings if os.path.isdir(each)]
    if not others:
        pytest.skip("this filesystem tells case and Unicode forms apart")
    m = JobManager()
    gate, run = gated()

    first = m.create_threaded("demo", "First", run, folder=str(folder))
    same = [m.create_threaded("demo", each, run, folder=each) for each in others]
    statuses = [job.status for job in same]
    gate.set()
    for job in (first, *same):
        await finished(job)

    assert statuses == ["queued"] * len(others)


def test_a_full_queue_refuses_a_job_with_429(client, album_dir, integrity_stub, monkeypatch):
    monkeypatch.setattr(jobs_mod, "MAX_QUEUED_JOBS", 0)
    resp = client.post("/api/checks/run", json={"path": str(album_dir), "checks": ["integrity"]})
    assert resp.status_code == 429
    assert resp.json()["detail"] == "0 jobs are already waiting: try again once some have run."
    assert manager.jobs == {}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def test_what_a_job_prints_goes_to_its_log_and_nothing_else_does():
    async def run(job):
        click.echo(f"echo {job.title}")
        click.secho(f"secho {job.title}", fg="green", bold=True)
        click.echo(f"err {job.title}", err=True)
        print(f"print {job.title}")
        await asyncio.to_thread(click.echo, f"worker {job.title}")
        click.echo("part, ", nl=False)
        click.echo("whole")
        click.echo("10%\r50%\r100%")
        click.echo("unfinished", nl=False)

    # Streams of the test's own, set here: pytest puts its sys.stdout back after each phase, and closes capsys's.
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), web_client() as c:
        print("the server's own line")
        assert c.portal is not None
        a, b = (c.portal.call(lambda name=name: manager.create_threaded("prints", name, run)) for name in "ab")
        logs = {name: join_job(c, job.id)["log"] for name, job in (("a", a), ("b", b))}

    for name, log in logs.items():
        assert log == [
            f"echo {name}",
            f"secho {name}",
            f"err {name}",
            f"print {name}",
            f"worker {name}",
            "part, whole",
            "100%",
            "unfinished",
        ]
    assert "the server's own line" in out.getvalue()
    assert "echo a" not in out.getvalue()
    assert "err a" not in err.getvalue()


def test_stopping_the_server_cancels_its_jobs():
    async def waits(job):
        await asyncio.Event().wait()

    with web_client() as c:
        assert c.portal is not None
        jobs = [c.portal.call(manager.create_threaded, "demo", "Waits", waits) for _ in range(manager.max_jobs + 1)]
        assert [job.status for job in jobs] == ["running"] * manager.max_jobs + ["queued"]

    # Stopped with the server: the running jobs and the one waiting its turn.
    assert [job.status for job in jobs] == ["cancelled"] * (manager.max_jobs + 1)


def test_what_a_jobs_worker_writes_as_the_server_stops_still_goes_to_its_log():
    started, release = threading.Event(), threading.Event()

    def worker() -> None:
        started.set()
        release.wait(5)
        print("late line")

    async def body(job):
        await asyncio.to_thread(worker)

    out = io.StringIO()
    with contextlib.redirect_stdout(out), web_client() as c:
        assert c.portal is not None
        job = c.portal.call(manager.create_threaded, "demo", "Late", body)
        worker_started = started.wait(5)
        assert worker_started
        # The worker writes after the server has begun to stop and cancelled its job.
        threading.Timer(0.3, release.set).start()

    assert "late line" in job.log_lines
    assert "late line" not in out.getvalue()


def test_a_worker_still_running_once_the_server_has_stopped_writes_nothing_to_the_terminal(monkeypatch):
    monkeypatch.setattr(jobs_mod, "STOP_TIMEOUT", 0.2)
    started, release = threading.Event(), threading.Event()

    def worker() -> None:
        started.set()
        release.wait(10)
        print("late line")

    async def body(job):
        await asyncio.to_thread(worker)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        with web_client() as c:
            assert c.portal is not None
            job = c.portal.call(manager.create_threaded, "demo", "Late", body)
            worker_started = started.wait(5)
            assert worker_started
        # The server stopped without it: its worker writes only now.
        release.set()
        assert job.thread is not None
        job.thread.join(10)
        ended = not job.thread.is_alive()
        # As the process ending would: the kept capture must not be undone later, in another test.
        kept = cast("FastAPI", c.app).state.kept_capture
        kept.close()

    assert ended
    assert "late line" in job.log_lines
    assert "late line" not in out.getvalue()


@pytest.mark.parametrize(
    "error",
    [
        UnknownOutcomeError("Network error: Server disconnected"),
        ExceptionGroup("group", [UnknownOutcomeError("Server error 502")]),
        RuntimeError("upload failed"),
        ValueError("its answer was not JSON"),
    ],
)
async def test_an_unknown_outcome_gets_a_status_of_its_own(error):
    async def run(job):
        if isinstance(error, RuntimeError):
            # Raised from an unknown outcome.
            try:
                raise UnknownOutcomeError("Network error: timed out")
            except UnknownOutcomeError as cause:
                raise error from cause
        if isinstance(error, ValueError):
            # Raised while handling one, with no `from`.
            try:
                raise UnknownOutcomeError("Network error: timed out")
            except UnknownOutcomeError:
                raise error  # noqa: B904
        raise error

    job = await finished(JobManager().create_threaded("unknown", "Unknown", run))
    assert job.status == "unknown_outcome"
    assert job.error is not None
    assert job.error.startswith(UNKNOWN_OUTCOME)


@pytest.mark.parametrize("behind", ["the context, under a cause", "a group, as the cause", "the first of two"])
async def test_the_first_unknown_outcome_anywhere_behind_the_error_is_found(behind):
    async def run(job):
        lost = UnknownOutcomeError("Network error: timed out")
        if behind == "a group, as the cause":
            raise RuntimeError("upload failed") from ExceptionGroup("tasks", [lost])
        if behind == "the first of two":
            first = RuntimeError("upload failed")
            first.__cause__ = lost
            raise ExceptionGroup("tasks", [first, UnknownOutcomeError("Server error 502")])
        try:
            raise lost
        except UnknownOutcomeError:
            raise RuntimeError("upload failed") from ValueError("its answer was not JSON")

    job = await finished(JobManager().create_threaded("unknown", "Unknown", run))
    assert (job.status, job.error) == ("unknown_outcome", f"{UNKNOWN_OUTCOME} (Network error: timed out)")


async def test_an_error_chain_that_loops_back_ends_failed():
    async def run(job):
        first, second = RuntimeError("first"), ValueError("second")
        first.__cause__, second.__context__ = second, first
        raise first

    job = await finished(JobManager().create_threaded("demo", "Demo", run))
    assert job.status == "failed"


@pytest.mark.parametrize("then", ["returns", "aborts", "is cancelled"])
async def test_a_job_that_went_on_past_an_unknown_outcome_ends_as_one(then):
    noted = threading.Event()

    async def run(job):
        record.note_unknown_outcome(UnknownOutcomeError("the lossy report went unanswered"))
        noted.set()
        if then == "aborts":
            raise click.Abort()
        if then == "is cancelled":
            await asyncio.sleep(15)
        return "result"

    m = JobManager()
    job = m.create_threaded("upload", "Upload", run)
    await until(noted.is_set, "the job to note its unknown outcome")
    if then == "is cancelled":
        cancelled = m.cancel(job.id)
        assert cancelled is True
    await finished(job)
    expected = ("unknown_outcome", f"{UNKNOWN_OUTCOME} (the lossy report went unanswered)", None)
    assert (job.status, job.error, job.result) == expected


@pytest.mark.parametrize(
    ("stops_on", "named"),
    [
        ("another", "the lossy report went unanswered; the upload went unanswered"),
        ("the noted one", "the lossy report went unanswered"),
    ],
)
async def test_a_job_that_stops_on_an_unknown_outcome_names_it_after_those_it_noted(stops_on, named):
    async def run(job):
        report = UnknownOutcomeError("the lossy report went unanswered")
        record.note_unknown_outcome(report)
        # As a cross-upload stops once its next upload goes unanswered.
        lost = UnknownOutcomeError("the upload went unanswered") if stops_on == "another" else report
        raise click.exceptions.Exit(1) from lost

    job = await finished(JobManager().create_threaded("upload", "Upload", run))
    assert (job.status, job.error) == ("unknown_outcome", f"{UNKNOWN_OUTCOME} ({named})")


async def test_each_job_records_its_own_unknown_outcomes():
    noted = threading.Event()

    async def lost(job):
        await asyncio.to_thread(record.note_unknown_outcome, UnknownOutcomeError("lost"))
        noted.set()

    async def clean(job):
        # Ends after the other job's note: a record they shared would end it as an unknown outcome too.
        await asyncio.to_thread(noted.wait, 15)
        return "result"

    m = JobManager()
    first, second = m.create_threaded("upload", "Lost", lost), m.create_threaded("upload", "Clean", clean)
    await finished(first)
    await finished(second)
    assert (first.status, second.status, second.result) == ("unknown_outcome", "done", "result")


async def test_a_job_whose_awaited_task_was_cancelled_ends_cancelled_with_no_cancel_asked():
    async def run(job):
        inner = asyncio.create_task(asyncio.sleep(15))
        asyncio.get_running_loop().call_soon(inner.cancel)
        await inner

    job = await finished(JobManager().create_threaded("demo", "Demo", run))
    assert (job.status, job.error) == ("cancelled", None)


async def test_a_cancelled_job_whose_request_went_unanswered_ends_as_an_unknown_outcome():
    async def run(job):
        try:
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            # As a request handed over does once its answer is lost: the outcome, not the cancel.
            raise UnknownOutcomeError("Network error: timed out") from None

    m = JobManager()
    job = m.create_threaded("unknown", "Unknown", run)
    await task_started(job)
    cancelled = m.cancel(job.id)
    await finished(job)
    assert cancelled is True
    assert (job.status, job.error) == ("unknown_outcome", f"{UNKNOWN_OUTCOME} (Network error: timed out)")


def test_cancelling_a_job_during_its_post_stops_it_once_the_tracker_answered():
    tracker = FakeTracker()
    went_on = threading.Event()

    with web_client() as c:
        assert c.portal is not None
        site = c.portal.call(tracker.start)

        async def run(job):
            (api,) = _clients(site, "RED", unlimited=True)
            try:
                await _post(api)
                went_on.set()
            finally:
                await api.close()

        try:
            job = c.portal.call(manager.create_threaded, "posts", "Posts", run)
            deadline = time.monotonic() + 15
            while not tracker.hits and time.monotonic() < deadline:
                time.sleep(0.005)
            cancel = c.post(f"/api/jobs/{job.id}/cancel", json={})
            data = join_job(c, job.id)
        finally:
            c.portal.call(tracker.runner.cleanup)

    assert cancel.status_code == 200
    assert data["status"] == "cancelled"
    # The job stopped once the tracker had answered, and went no further.
    assert tracker.posts == 0
    assert not went_on.is_set()
    assert any(line.startswith("Cancelled once POST /ajax.php?action=upload to RED") for line in data["log"])
    assert [method for _, _, method, _ in tracker.hits] == ["POST"]


def test_a_failed_job_says_why_and_its_traceback_goes_to_the_servers_stderr():
    async def run(job):
        click.echo("working")
        raise RuntimeError("it broke")

    err = io.StringIO()
    with contextlib.redirect_stderr(err), web_client() as c:
        assert c.portal is not None
        job = c.portal.call(lambda: manager.create_threaded("fails", "Fails", run))
        data = join_job(c, job.id)

    assert data["status"] == "failed"
    assert data["error"] == "RuntimeError: it broke"
    assert data["log"] == ["working"]
    assert "Traceback (most recent call last)" in err.getvalue()
    assert "RuntimeError: it broke" in err.getvalue()


def test_a_failed_jobs_traceback_on_the_servers_stderr_is_masked_through_its_chain(monkeypatch):
    api_key, passkey, authkey = "fake-api-key-0123456789", "fakepasskey0123456789abcdef", "fake-authkey-0123456789"
    monkeypatch.setattr(egress, "_configured_secrets", lambda: (api_key,))
    monkeypatch.setattr(tracker_base, "_learned_secrets", {passkey})

    async def run(job):
        try:
            raise KeyError(api_key)
        except KeyError as e:
            raise RuntimeError(f"GET https://tracker.invalid/ajax.php?authkey={authkey} for {passkey}") from e

    err = io.StringIO()
    with contextlib.redirect_stderr(err), web_client() as c:
        assert c.portal is not None
        job = c.portal.call(lambda: manager.create_threaded("fails", "Fails", run))
        data = join_job(c, job.id)

    assert data["status"] == "failed"
    shown = err.getvalue()
    assert "KeyError" in shown
    assert "RuntimeError: GET https://tracker.invalid/ajax.php?authkey=" in shown
    for secret in (api_key, passkey, authkey):
        assert secret not in shown


def test_an_unknown_outcomes_traceback_goes_to_the_servers_stderr_masked(monkeypatch):
    api_key = "fake-api-key-0123456789"
    monkeypatch.setattr(egress, "_configured_secrets", lambda: (api_key,))

    async def run(job):
        try:
            raise KeyError(api_key)
        except KeyError as e:
            raise UnknownOutcomeError("Network error: timed out") from e

    err = io.StringIO()
    with contextlib.redirect_stderr(err), web_client() as c:
        assert c.portal is not None
        job = c.portal.call(lambda: manager.create_threaded("unknown", "Unknown", run))
        data = join_job(c, job.id)

    assert data["status"] == "unknown_outcome"
    shown = err.getvalue()
    assert "KeyError" in shown
    assert "UnknownOutcomeError: Network error: timed out" in shown
    assert api_key not in shown


# ---------------------------------------------------------------------------
# The Host check (DNS rebinding)
# ---------------------------------------------------------------------------

HOST_REFUSED = {"detail": "This host name is not allowed. Add it to allowed_hosts in the [web] config section."}


def test_default_loopback_bind_rejects_testserver_host_header():
    c = web_client(base_url="http://testserver")
    resp = c.get("/api/jobs")
    assert resp.status_code == 403
    assert resp.json() == HOST_REFUSED


def test_loopback_bind_rejects_evil_host_header(client):
    resp = client.get("/api/jobs", headers={"host": "evil.example.com"})
    assert resp.status_code == 403
    assert resp.json() == HOST_REFUSED


def test_a_wildcard_bind_checks_the_host_too(monkeypatch):
    # Upstream's rule: 0.0.0.0 names no machine, so a LAN bind needs its names in allowed_hosts.
    monkeypatch.setattr(cfg.web, "allowed_hosts", [])
    refused = web_client(base_url="http://testserver", host=LAN_BIND).get("/api/jobs")
    monkeypatch.setattr(cfg.web, "allowed_hosts", ["testserver"])
    allowed = web_client(base_url="http://testserver", host=LAN_BIND).get("/api/jobs")
    assert refused.status_code == 403
    assert allowed.status_code == 200


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
    ended = join_job(client, job_id)
    assert ended["status"] == "done"

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


def test_checks_job_keeps_the_source_it_was_verified_for(client, album_dir, integrity_stub):
    """The Upload page rebuilds its verified-for signature from these params when you come back to it."""
    body = {"path": str(album_dir), "checks": ["integrity"], "source": "WEB", "trackers": []}
    job_id = client.post("/api/checks/run", json=body).json()["id"]
    finished = join_job(client, job_id)

    assert finished["params"]["source"] == "WEB"


def test_job_factory_error_is_surfaced_in_job_dict(client, album_dir, monkeypatch):
    async def broken_check_integrity(path):
        raise ValueError("integrity exploded")

    monkeypatch.setattr("salmon.checks.album_web.check_integrity", broken_check_integrity)
    resp = client.post("/api/checks/run", json={"path": str(album_dir), "checks": ["integrity"]})
    assert resp.status_code == 200
    data = join_job(client, resp.json()["id"])
    assert data["status"] == "failed"
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
    ended = join_job(client, resp.json()["id"])
    assert ended["status"] == "done"
    # The vetted destination is the one handed to create_specs_folder, and it is outside the library.
    assert specs_requests == [get_spectrals_path(str(library_album))]
    assert not cfg.directory.is_library_path(specs_requests[0])


def test_spectrals_of_a_library_album_never_go_inside_it(library_album, monkeypatch):
    from salmon.uploader.spectrals import get_spectrals_path

    # No tmp_dir: an ordinary album would get <album>/Spectrals, a library album a folder in download_directory.
    monkeypatch.setattr(cfg.directory, "tmp_dir", "")
    spectrals_path = pathlib.Path(get_spectrals_path(str(library_album)))
    assert not spectrals_path.is_relative_to(library_album)
    assert spectrals_path.parent == pathlib.Path(cfg.directory.download_directory)


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
    assert data["result"]["spectral_ids"] == {"1": "01. Intro.flac", "2": "02. Mittelteil.flac"}
    # Frequency plots are shown but never posted, so they stay out of "files".
    assert data["result"]["files"] == ["01-intro.png", "02-mittelteil.png"]
    assert data["result"]["frequency"][0]["image"] == "01 Spectrum.png"
    assert data["result"]["assessment"] == {"level": "none", "notes": ["No track carried enough signal to measure."]}


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

    resp = client.get(f"/api/spectrals/{job_id}/image/01%20Spectrum.png")

    assert resp.status_code == 200
    assert resp.content == PNGF


def test_spectrals_generate_second_post_on_same_path_waits_its_turn(client, album_dir, hanging_spectral_stubs):
    first = client.post("/api/spectrals/generate", json={"path": str(album_dir)})
    assert first.status_code == 200
    job_id = first.json()["id"]

    second = client.post("/api/spectrals/generate", json={"path": str(album_dir)})
    assert second.status_code == 200
    new_id = second.json()["id"]
    assert second.json()["status"] == "queued"

    # Once the running job ends, the waiting one starts.
    wait_task(job_id)
    cancelled = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"cancelled": job_id}
    ended = join_job(client, job_id)
    assert ended["status"] == "cancelled"
    wait_task(new_id)
    assert manager.jobs[new_id].status == "running"
    client.post(f"/api/jobs/{new_id}/cancel")
    ended = join_job(client, new_id)
    assert ended["status"] == "cancelled"


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
    assert resp.content == PNG1
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


def test_a_spectral_that_is_not_an_image_is_not_uploaded(client, album_dir, spectral_stubs, monkeypatch):
    uploaded = []

    async def fake_upload_images(filepaths, image_host):
        uploaded.extend(filepaths)
        return ["https://img.example/0" for _ in filepaths]

    monkeypatch.setattr("salmon.webui.routers.spectrals.upload_images", fake_upload_images)
    gen_id = run_generate_to_done(client, album_dir)
    # Swapped after the spectrals were made: the upload checks what it sends, not what was made.
    (spectral_stubs / "02-mittelteil.png").write_bytes(b"d8:announce30:https://tracker.example/announcee")

    resp = client.post("/api/spectrals/upload", json={"job_id": gen_id, "host": "catbox"})
    data = join_job(client, resp.json()["id"])

    assert data["status"] == "failed"
    assert "Not uploading 02-mittelteil.png" in data["error"]
    assert uploaded == []


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

    async def fake_transcode_folder(path, bitrate, output_dir=None):
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


def test_transcode_and_downconvert_on_one_folder_take_turns(client, album_dir, monkeypatch):
    async def hang_transcode(path, bitrate, output_dir=None):
        await asyncio.Event().wait()

    async def fake_convert_folder(path, output_dir=None):
        return 44100, f"{path} [16-44]"

    monkeypatch.setattr("salmon.webui.routers.convert.transcode_folder", hang_transcode)
    monkeypatch.setattr("salmon.webui.routers.convert.convert_folder", fake_convert_folder)

    first = client.post("/api/convert/transcode", json={"path": str(album_dir), "bitrate": "320"})
    assert first.status_code == 200
    job_id = first.json()["id"]

    # A job on the same album folder waits, whichever endpoint started it.
    after = client.post("/api/convert/downconvert", json={"path": str(album_dir)})
    assert after.status_code == 200
    assert after.json()["status"] == "queued"

    wait_task(job_id)
    client.post(f"/api/jobs/{job_id}/cancel")
    ended = join_job(client, job_id)
    assert ended["status"] == "cancelled"
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
    assert data["result"]["raw"]["integrity"] == {
        "passed": True,
        "details": "ok",
        "concerns": [],
        "md5_unset": [],
        "decode_failures": [],
        "checked": 0,
    }
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
    headers = {"host": "localhost", "origin": "http://localhost"}
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


def test_discarding_drops_salmons_claim_on_the_folder(tmp_path, monkeypatch):
    from salmon.uploader.spectrals import create_specs_folder, made_by_salmon
    from salmon.webui.routers.spectrals import _discard_spectrals

    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_path))
    specs = pathlib.Path(create_specs_folder("", os.path.join(os.path.realpath(tmp_path), "spectrals_claim")))
    _discard_spectrals(_finished_spectrals_job(specs))
    specs.mkdir()

    assert not made_by_salmon(str(specs))


def test_discarding_refuses_a_path_outside_the_configured_directories(tmp_path):
    from salmon.webui.routers.spectrals import _discard_spectrals

    outside = _specs_under(tmp_path, "spectrals_elsewhere")
    _discard_spectrals(_finished_spectrals_job(outside))
    assert outside.exists(), "rmtree must not follow a result path that has left the roots"


def test_discarding_leaves_a_folder_holding_another_file(tmp_path, monkeypatch):
    from salmon.webui.routers.spectrals import _discard_spectrals

    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_path))
    specs = _specs_under(tmp_path, "spectrals_x")
    (specs / "notes.txt").write_bytes(b"theirs")
    _discard_spectrals(_finished_spectrals_job(specs))
    assert (specs / "notes.txt").read_bytes() == b"theirs"


def test_discarding_leaves_another_folder_the_jobs_path_now_links_to(tmp_path, monkeypatch):
    from salmon.webui.routers.spectrals import _discard_spectrals

    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_path))
    other = _specs_under(tmp_path, "spectrals_other")
    link = pathlib.Path(os.path.realpath(tmp_path)) / "spectrals_x"
    link.symlink_to(other)
    _discard_spectrals(_finished_spectrals_job(link))
    assert (other / "01 Full.png").exists()


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
    refused = client.delete("/api/spectrals/job-does-not-exist")
    assert refused.status_code == 404


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


def test_spectrals_cannot_be_pushed_to_reds_image_host(client, album_dir, spectral_stubs):
    """The config refuses to load with this set; a request must not reach around it."""
    job_id = client.post("/api/spectrals/generate", json={"path": str(album_dir)}).json()["id"]
    join_job(client, job_id)

    resp = client.post("/api/spectrals/upload", json={"job_id": job_id, "host": "red"})

    assert resp.status_code == 422
    assert "does not allow spectral uploads" in resp.json()["detail"]
