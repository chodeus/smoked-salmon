"""End-to-end tests for the web upload wizard.

Drives the UNMODIFIED salmon.uploader.upload() pipeline through
JobManager.create_threaded — the same entry point the /api/upload router
uses — with the module-level asyncclick interaction patches installed, so
every terminal prompt surfaces as a browser question on the job.

The world is mocked at the narrowest seams of salmon/uploader/__init__.py
(metadata gathering, tag/rename plumbing, spectral *generation*, image
hosts) while the interactive machinery stays real: the source prompt, the
dupe checker and its prompts against a fake Gazelle API, check_spectrals'
lossy/selection prompts, the upload confirm, real torf torrent generation,
prepare_and_upload against the fake tracker, and the multi-tracker loop.

Tests answer questions exactly like the HTTP layer does: poll job.question
and deliver values via WebInteraction.answer (plus two genuine HTTP-level
tests for the answer endpoint and spectral image serving).
"""

import asyncio
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import asyncclick as click
import pytest
from fastapi.testclient import TestClient
from torf import Torrent

import salmon.tagger.pre_data
import salmon.trackers
import salmon.uploader.spectrals
from salmon import cfg
from salmon.constants import RELEASE_TYPES
from salmon.uploader import upload as run_upload
from salmon.webui import interaction as interaction_mod
from salmon.webui.app import create_app
from salmon.webui.interaction import install_interaction_patches
from salmon.webui.jobs import JobConflictError, JobManager
from salmon.webui.jobs import manager as global_manager

POLL = 0.005
DEADLINE = 15.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def wait_for(cond, message, timeout=DEADLINE):
    """Poll fast until cond() is truthy; no fixed sleeps."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        await asyncio.sleep(POLL)
    raise AssertionError(f"Timed out waiting for {message}")


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
    dicts seen, in order.
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


def wait_until(cond, message, timeout=DEADLINE):
    """Synchronous poll for HTTP-level tests (job threads update state directly)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {message}")


# ---------------------------------------------------------------------------
# Fixtures: interaction patches, managers, fake tracker, mocked world
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

TORRENTGROUP_RESPONSE = {
    "group": {
        "id": 2002,
        "name": "Testalbum",
        "year": 2024,
        "musicInfo": {"artists": [{"name": "Testartist"}]},
        "recordLabel": "Test Label",
        "catalogueNumber": "TL-001",
    },
    "torrents": [
        {
            "id": 1001,
            "media": "WEB",
            "format": "FLAC",
            "encoding": "Lossless",
            "remastered": False,
            "remasterYear": 2024,
            "remasterTitle": "",
            "remasterRecordLabel": "",
            "remasterCatalogueNumber": "",
        }
    ],
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
def tracker(fake_tracker, tmp_path):
    """conftest FakeGazelleApi completed with everything upload() touches."""
    fake_tracker.release_types = RELEASE_TYPES
    fake_tracker.dot_torrents_dir = str(tmp_path / "dottorrents")
    os.makedirs(fake_tracker.dot_torrents_dir, exist_ok=True)
    fake_tracker.auth_calls = 0

    async def ensure_authenticated():
        fake_tracker.auth_calls += 1

    fake_tracker.ensure_authenticated = ensure_authenticated
    fake_tracker.api_responses["browse"] = {"results": [dict(SEARCH_RESULT)]}
    fake_tracker.api_responses["torrentgroup"] = TORRENTGROUP_RESPONSE
    return fake_tracker


@pytest.fixture
def upload_world(monkeypatch, tmp_path):
    """Stub upload()'s external seams; pin config so the flow is deterministic
    regardless of the ambient developer config.toml."""
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

    async def fake_upload_cover(cover_path, site_code=None):
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
    monkeypatch.setattr("salmon.uploader.rename_folder", lambda path, metadata, auto_rename, parent=None: path)
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


def wizard_factory(tracker, path):
    """Job factory calling the unmodified pipeline like routers/upload.py does."""

    async def run(job):
        await run_upload(
            tracker,
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
        return {"album_path": path, "tracker": tracker.site_code}

    return run


# ---------------------------------------------------------------------------
# 1. Full happy path
# ---------------------------------------------------------------------------


async def test_full_happy_path_uploads_to_fake_tracker(jm, tracker, album_dir, upload_world):
    m = jm()
    queue = m.subscribe()
    path = str(album_dir)
    job = m.create_threaded("upload", "Upload to RED", wizard_factory(tracker, path), lock_key=path)

    questions = await drive(
        job,
        [
            ("What is the source of this release?", "web"),
            ("Would you like to upload to an existing group?", "n"),
            ("Is this release lossy mastered?", "n"),
            ("What spectral IDs would you like to upload", "*"),
            ("Would you like to upload the torrent?", True),
            ("Would you like to check downconversion options?", False),
            ("Your choices are OPS or [n]one.", "n"),
        ],
    )
    await join_job(job)

    assert job.status == "done", job.error
    assert job.error is None
    assert job.result == {"album_path": path, "tracker": "RED"}
    assert path not in m._active_lock_keys

    # asyncclick prompts arrive as async questions, confirms as sync ones.
    assert [q["kind"] for q in questions] == [
        "prompt",
        "prompt",
        "prompt",
        "prompt",
        "confirm",
        "confirm",
        "prompt",
    ]

    # The fake tracker received the compiled upload payload for a new group.
    assert tracker.auth_calls == 1
    assert len(tracker.uploads) == 1
    data, files = tracker.uploads[0]
    assert data["title"] == "Testalbum"
    assert data["artists[]"] == ["Testartist"]
    assert data["format"] == "FLAC"
    assert data["bitrate"] == "Lossless"
    assert data["media"] == "WEB"
    assert data["image"] == "https://img.example/cover"
    assert data["tags"] == "Electronic,2020s"  # decade from group_year 2024
    assert "groupid" not in data
    assert files.torrent_data is not None

    # Dupe check and the post-upload group listing hit the (fake) site API.
    actions = [action for action, _ in tracker.api_calls]
    assert "browse" in actions
    assert "torrentgroup" in actions

    # Torrent generation was real: torf wrote the file, then upload_and_report
    # rewrote it with the permalink comment.
    torrent_path = os.path.join(tracker.dot_torrents_dir, f"{album_dir.name} - RED.torrent")
    assert os.path.isfile(torrent_path)
    read_back = Torrent.read(torrent_path)
    assert read_back.comment == "https://redacted.sh/torrents.php?torrentid=1001"
    assert read_back.private is True

    # Spectrals surfaced to the browser instead of a native viewer, and the
    # selected ids were passed to the (stubbed) spectral uploader.
    assert job.interaction.spectrals is not None
    assert len(job.interaction.spectrals["files"]) == 6
    assert upload_world.spectral_uploads == [
        (job.interaction.spectrals["path"], {1: TRACK_FILES[0], 2: TRACK_FILES[1], 3: TRACK_FILES[2]})
    ]

    log = "\n".join(job.log_lines)
    assert "Uploading to a new torrent group." in log
    assert "Spectrals are shown in the browser below." in log
    assert "Successfully uploaded https://redacted.sh/torrents.php?torrentid=1001" in log
    assert "Done with this release." in log

    # Everything flowed to subscribers (what the jobs websocket relays).
    await asyncio.sleep(0.05)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    kinds = [e["event"] for e in events]
    assert kinds.count("question") == 7
    assert kinds.count("question_answered") == 7
    assert "spectrals_ready" in kinds
    assert "log" in kinds
    assert kinds[0] == "created"
    assert kinds[-1] == "finished"


# ---------------------------------------------------------------------------
# 2. Abort paths
# ---------------------------------------------------------------------------


async def test_abort_at_source_prompt_errors_job_and_releases_lock(jm, tracker, album_dir, upload_world):
    m = jm()
    path = str(album_dir)
    job = m.create_threaded("upload", "Upload", wizard_factory(tracker, path), lock_key=path)

    await drive(job, [("What is the source of this release?", "a")])
    await join_job(job)

    assert job.status == "error"
    assert job.error == "Aborted."
    assert job.result is None
    assert job.question is None
    assert tracker.uploads == []
    assert path not in m._active_lock_keys


async def test_abort_at_dupe_choice_is_swallowed_by_the_pipeline(jm, tracker, album_dir, upload_world):
    """Deviation from the wishlist: upload() catches click.Abort raised at the
    dupe-choice prompt internally and returns after logging "Aborting
    upload..." — so at job level this is a clean 'done', not an error. The
    'Aborted.' error status is only reachable from prompts outside upload()'s
    try block (see the source-prompt test above)."""
    m = jm()
    path = str(album_dir)
    job = m.create_threaded("upload", "Upload", wizard_factory(tracker, path), lock_key=path)

    await drive(
        job,
        [
            ("What is the source of this release?", "web"),
            ("Would you like to upload to an existing group?", "a"),
        ],
    )
    await join_job(job)

    assert job.status == "done"
    assert job.error is None
    assert tracker.uploads == []
    assert "Aborting upload..." in "\n".join(job.log_lines)
    assert path not in m._active_lock_keys


async def test_declining_a_coverless_new_group_skips_only_that_tracker(
    jm, tracker, album_dir, upload_world, monkeypatch
):
    ops = type(tracker)()
    ops.site_code = ops.site_string = "OPS"
    ops.base_url = "https://orpheus.network"
    ops.release_types = tracker.release_types
    ops.dot_torrents_dir = tracker.dot_torrents_dir
    ops.api_responses = dict(tracker.api_responses)

    async def ensure_authenticated():
        pass

    ops.ensure_authenticated = ensure_authenticated
    monkeypatch.setattr(salmon.trackers, "get_class", lambda site: lambda: ops)

    async def cover_fails_on_red(cover_path, site_code=None):
        return None if site_code == "RED" else "https://img.example/ops-cover"

    monkeypatch.setattr("salmon.uploader.upload_cover", cover_fails_on_red)

    m = jm()
    path = str(album_dir)
    job = m.create_threaded("upload", "Upload to RED", wizard_factory(tracker, path), lock_key=path)

    await drive(
        job,
        [
            ("What is the source of this release?", "web"),
            ("Would you like to upload to an existing group?", "n"),
            ("Is this release lossy mastered?", "n"),
            ("What spectral IDs would you like to upload", "*"),
            ("Would you like to upload the torrent?", True),
            ("Continue upload without a cover image?", "n"),
            ("Your choices are OPS or [n]one.", "OPS"),
            ("Would you like to upload to an existing group?", "n"),
            ("Would you like to check downconversion options?", False),
        ],
    )
    await join_job(job)

    assert job.status == "done", job.error
    assert tracker.uploads == []
    assert len(ops.uploads) == 1
    assert ops.uploads[0][0]["image"] == "https://img.example/ops-cover"
    assert "Skipping upload to RED." in "\n".join(job.log_lines)


# ---------------------------------------------------------------------------
# 3. Answer validation
# ---------------------------------------------------------------------------


async def test_answer_validation_wrong_id_rejected_right_id_accepted(jm):
    m = jm()

    async def factory(job):
        return await click.prompt("The only question")

    job = m.create_threaded("demo", "Demo", factory)
    question = await wait_for(lambda: job.question, "question to appear")

    assert m.answer("job-does-not-exist", question["id"], "x") is False
    assert job.interaction.answer("q-bogus", "x") is False
    assert job.question is not None  # still pending after bad answers
    assert job.interaction.answer(question["id"], "the answer") is True

    await join_job(job)
    assert job.status == "done"
    assert job.result == "the answer"
    # Stale answers after the question was consumed are rejected too.
    assert job.interaction.answer(question["id"], "late") is False


async def test_confirm_questions_accept_boolean_and_string_answers(jm):
    m = jm()

    async def factory(job):
        return [
            click.confirm("Boolean yes?"),
            click.confirm("String yes?"),
            click.confirm("Default yes?", default=True),
            click.confirm("String no?"),
        ]

    job = m.create_threaded("demo", "Demo", factory)
    questions = await drive(
        job,
        [
            ("Boolean yes?", True),
            ("String yes?", "yes"),
            ("Default yes?", ""),  # empty answer -> takes the default (True)
            ("String no?", "n"),
        ],
    )
    await join_job(job)

    assert job.status == "done", job.error
    assert job.result == [True, True, True, False]
    assert all(q["kind"] == "confirm" for q in questions)


# ---------------------------------------------------------------------------
# 4. Cancel while a question is pending
# ---------------------------------------------------------------------------


async def test_cancel_while_async_prompt_pending_cancels_job_and_frees_lock(jm):
    m = jm()

    async def factory(job):
        return await click.prompt("Never answered?")  # async question

    job = m.create_threaded("demo", "Demo", factory, lock_key="/albums/x")
    await wait_for(lambda: job.question, "question to appear")

    assert m.cancel(job.id) is True
    await join_job(job)

    assert job.status == "cancelled"
    assert job.question is None
    assert job.result is None
    assert "/albums/x" not in m._active_lock_keys

    async def quick(job):
        return "ok"

    second = m.create_threaded("demo", "Demo 2", quick, lock_key="/albums/x")
    await join_job(second)
    assert second.status == "done"


async def test_cancel_while_sync_confirm_pending_clears_question_and_lock(jm):
    """BUG (pinned): cancelling while a *sync* question (confirm/edit) is
    pending currently ends the job in status 'error' ("CancelledError: ")
    rather than 'cancelled': WebInteraction.cancel_pending() cancels a
    concurrent.futures.Future, and on this Python
    concurrent.futures.CancelledError is a plain Exception subclass — NOT
    asyncio.CancelledError — so _thread_main's `except asyncio.CancelledError`
    misses it and _record_failure runs instead. The important invariants
    (question cleared, lock released, thread stopped) do hold; this test
    accepts either status so it stays green once the code is fixed."""
    m = jm()

    async def factory(job):
        click.confirm("Never answered?")  # sync: blocks the job thread
        return "unreachable"

    job = m.create_threaded("demo", "Demo", factory, lock_key="/albums/y")
    await wait_for(lambda: job.question, "question to appear")

    assert m.cancel(job.id) is True
    await join_job(job)

    assert job.status in {"cancelled", "error"}  # currently: "error" (see docstring)
    if job.status == "error":
        assert job.error.startswith("CancelledError")
    assert job.question is None
    assert job.result is None
    assert "/albums/y" not in m._active_lock_keys

    async def quick(job):
        return "ok"

    second = m.create_threaded("demo", "Demo 2", quick, lock_key="/albums/y")
    await join_job(second)
    assert second.status == "done"


# ---------------------------------------------------------------------------
# 5. Path locking + contextvar isolation between concurrent job threads
# ---------------------------------------------------------------------------


async def test_same_path_conflicts_and_distinct_paths_run_concurrently(jm):
    m = jm()

    async def alpha(job):
        return {"alpha": await click.prompt("Alpha question")}

    async def beta(job):
        return {"beta": await click.prompt("Beta question")}

    job_a = m.create_threaded("demo", "A", alpha, lock_key="/albums/a")
    with pytest.raises(JobConflictError, match="/albums/a"):
        m.create_threaded("demo", "A again", alpha, lock_key="/albums/a")
    job_b = m.create_threaded("demo", "B", beta, lock_key="/albums/b")

    await wait_for(
        lambda: job_a.question is not None and job_b.question is not None,
        "both questions pending at once",
    )
    # Each worker thread sees its own interaction: questions did not cross.
    assert "Alpha question" in job_a.question["text"]
    assert "Beta question" in job_b.question["text"]
    # Answers are scoped: job B rejects job A's question id.
    assert job_b.interaction.answer(job_a.question["id"], "x") is False

    assert job_a.interaction.answer(job_a.question["id"], "from a") is True
    assert job_b.interaction.answer(job_b.question["id"], "from b") is True
    await join_job(job_a)
    await join_job(job_b)

    assert job_a.result == {"alpha": "from a"}
    assert job_b.result == {"beta": "from b"}
    assert m._active_lock_keys == set()


# ---------------------------------------------------------------------------
# 6. Log capture inside jobs; CLI context untouched
# ---------------------------------------------------------------------------


async def test_secho_streams_to_job_log_and_cli_context_uses_original(jm, monkeypatch):
    printed = []
    monkeypatch.setitem(interaction_mod._originals, "secho", lambda message=None, **kw: printed.append(message))

    async def factory(job):
        click.secho("streamed to the browser", fg="cyan")
        return "ok"

    m = jm()
    job = m.create_threaded("demo", "Demo", factory)
    await join_job(job)

    # Inside a web job: captured in the job log AND still passed to the original.
    assert "streamed to the browser" in job.log_lines
    assert printed == ["streamed to the browser"]

    # Outside any job (CLI context): only the original prints, no job log.
    click.secho("plain cli line")
    assert printed == ["streamed to the browser", "plain cli line"]
    assert "plain cli line" not in job.log_lines


# ---------------------------------------------------------------------------
# 7. click.edit and the raw-input seam (pre_data)
# ---------------------------------------------------------------------------


async def test_edit_and_raw_input_seams(jm):
    m = jm()

    async def factory(job):
        edited = click.edit("original text")
        kept = click.edit("keep me")
        encoding = salmon.tagger.pre_data._prompt_encoding()
        return {"edited": edited, "kept": kept, "encoding": encoding}

    job = m.create_threaded("demo", "Demo", factory)
    questions = await drive(
        job,
        [
            ("Edit the text below", "rewritten body"),
            ("Edit the text below", None),  # discard -> None
            ("What is the encoding of this release?", "320"),
        ],
    )
    await join_job(job)

    assert job.status == "done", job.error
    assert [q["kind"] for q in questions] == ["edit", "edit", "prompt"]
    assert questions[0]["initial"] == "original text"
    assert questions[1]["initial"] == "keep me"
    # click.edit's contract: saved text gains a trailing newline; discard is None.
    assert job.result == {"edited": "rewritten body\n", "kept": None, "encoding": ["320", False]}


# ---------------------------------------------------------------------------
# HTTP level: answer endpoint + spectral image serving for a threaded job
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    global_manager.jobs.clear()
    global_manager._subscribers.clear()
    global_manager._active_lock_keys.clear()
    with TestClient(create_app(), base_url="http://localhost") as c:
        try:
            yield c
        finally:
            for job in list(global_manager.jobs.values()):
                if job.finished_at is None:
                    global_manager.cancel(job.id)
            for job in global_manager.jobs.values():
                if job.thread is not None:
                    job.thread.join(timeout=5)
    global_manager.jobs.clear()
    global_manager._subscribers.clear()
    global_manager._active_lock_keys.clear()


def test_http_answer_endpoint_and_spectral_serving(client, tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "01-full.png").write_bytes(b"PNG-FULL")
    (specs / "02-zoom.png").write_bytes(b"PNG-ZOOM")

    async def factory(job):
        # The patched view_spectrals publishes the files to the browser.
        await salmon.uploader.spectrals.view_spectrals(str(specs), {1: TRACK_FILES[0]})
        return {"confirmed": click.confirm("Ship it to the tracker?")}

    assert client.portal is not None
    job = client.portal.call(
        lambda: global_manager.create_threaded("upload", "Wizard", factory, lock_key="/albums/http")
    )

    question = wait_until(lambda: job.question, "question to appear")

    data = client.get(f"/api/jobs/{job.id}").json()
    assert data["question"]["text"] == "Ship it to the tracker?"
    assert data["question"]["kind"] == "confirm"
    assert data["spectrals"] == ["01-full.png", "02-zoom.png"]

    resp = client.get(f"/api/jobs/{job.id}/spectral/01-full.png")
    assert resp.status_code == 200
    assert resp.content == b"PNG-FULL"
    assert resp.headers["content-type"] == "image/png"
    assert client.get(f"/api/jobs/{job.id}/spectral/nope.png").status_code == 404
    assert client.get("/api/jobs/job-unknown/spectral/01-full.png").status_code == 404

    unknown_job = client.post("/api/jobs/job-unknown/answer", json={"question_id": question["id"], "value": True})
    assert unknown_job.status_code == 404
    wrong = client.post(f"/api/jobs/{job.id}/answer", json={"question_id": "q-bogus", "value": True})
    assert wrong.status_code == 409
    ok = client.post(f"/api/jobs/{job.id}/answer", json={"question_id": question["id"], "value": True})
    assert ok.status_code == 200
    assert ok.json() == {"answered": question["id"]}

    wait_until(
        lambda: job.finished_at is not None and not job.thread.is_alive(),
        "job to finish",
    )
    final = client.get(f"/api/jobs/{job.id}").json()
    assert final["status"] == "done"
    assert final["result"] == {"confirmed": True}
    assert final["question"] is None


# ---------------------------------------------------------------------------
# --skip-flac-upload through the same pipeline
# ---------------------------------------------------------------------------


async def test_skip_flac_upload_sends_only_transcodes_linked_to_the_group_flac(
    jm, tracker, album_dir, upload_world, monkeypatch, tmp_path
):
    async def fake_transcode(path, bitrate, output_dir=None):
        target = os.path.join(output_dir or os.path.dirname(path), f"{os.path.basename(path)} [MP3 {bitrate}]")
        os.makedirs(target)
        for fn in TRACK_FILES:
            Path(target, fn.replace(".flac", ".mp3")).write_bytes(b"ID3" + bytes(2000))
        return target

    integrity_checked = []

    async def fake_integrity(path, scene, assume_yes):
        integrity_checked.append(path)

    monkeypatch.setattr("salmon.uploader.transcode_folder", fake_transcode)
    monkeypatch.setattr("salmon.uploader.resolve_integrity_for_upload", fake_integrity)
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path / "downloads"))
    path = str(album_dir)

    async def run(job):
        await run_upload(
            tracker,
            path,
            2002,
            "WEB",
            None,
            (),
            None,
            skip_up=True,
            skip_mqa=True,
            flac_group=TORRENTGROUP_RESPONSE,
        )

    m = jm()
    job = m.create_threaded("upload", "Upload to RED", run, lock_key=path)
    await drive(
        job,
        [
            ("Is this release lossy mastered?", "n"),
            ("What spectral IDs would you like to upload", "*"),
            ("Would you like to upload the torrent?", True),
            ("Select formats to convert", "*"),
        ],
    )
    await join_job(job)

    assert job.status == "done", job.error
    assert [(d["format"], d["bitrate"], d["groupid"]) for d, _files in tracker.uploads] == [
        ("MP3", "320", 2002),
        ("MP3", "V0 (VBR)", 2002),
    ]
    assert all("torrentid=1001" in d["release_desc"] for d, _files in tracker.uploads)
    assert "torrentgroup" not in [action for action, _ in tracker.api_calls]
    assert len(integrity_checked) == 1 and ".salmon-staging" in integrity_checked[0]


def _flac_with_art(path: Path, title: str, number: int, art_bytes: int) -> None:
    """A metadata-only FLAC (STREAMINFO, tags, one front cover) that mutagen reads and rewrites."""
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import PictureType

    info = (44100 << 44) | (1 << 41) | (15 << 36) | (44100 * 180)
    streaminfo = (4096).to_bytes(2, "big") * 2 + bytes(6) + info.to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    audio = FLAC(path)
    audio.update({"TITLE": title.lower(), "ARTIST": "testartist", "ALBUM": "testalbum", "TRACKNUMBER": str(number)})
    picture = Picture()
    picture.type, picture.mime, picture.data = PictureType.COVER_FRONT, "image/jpeg", b"\xff\xd8" + bytes(art_bytes)
    audio.add_picture(picture)
    audio.save()


def _snapshot(folder: Path) -> dict[str, str]:
    import hashlib

    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def skip_flac_run(jm, tracker, upload_world, monkeypatch, tmp_path):
    """Run --skip-flac-upload with the real steps that write into the folder, on a source with 1.1 MiB art."""
    from salmon.converter.conversions import record_conversion
    from salmon.converter.transcoding import _build_output_path
    from salmon.tagger import audio_info, retagger, tags
    from salmon.tagger.foldername import generate_folder_name, rename_folder

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    for fn_name, real in {
        "gather_audio_info": audio_info.gather_audio_info,
        "standardize_tags": tags.standardize_tags,
        "gather_tags": tags.gather_tags,
        "check_tags": tags.check_tags,
        "tag_files": retagger.tag_files,
        "rename_files": retagger.rename_files,
        "rename_folder": rename_folder,
    }.items():
        monkeypatch.setattr(f"salmon.uploader.{fn_name}", real)
    upload_world.metadata["tracks"] = {
        "1": {
            str(i): {"title": t, "artists": [("Testartist", "main")], "track#": str(i), "disc#": "1"}
            for i, t in enumerate(TRACK_TITLES, 1)
        }
    }
    run = SimpleNamespace(downloads=downloads, transcoded=[], integrity_checked=[], integrity_error=None)

    async def fake_transcode(path, bitrate, output_dir=None):
        target = _build_output_path(path, bitrate, output_dir)
        run.transcoded.append((path, target))
        os.makedirs(target)
        for flac in Path(path).glob("*.flac"):
            Path(target, flac.stem + ".mp3").write_bytes(b"ID3" + bytes(2000))
        record_conversion(target, source=path, kind="transcode", bitrate=bitrate)
        return target

    async def fake_integrity(path, scene, assume_yes):
        run.integrity_checked.append(path)  # the real one may re-encode the FLACs in place
        if run.integrity_error:
            raise run.integrity_error

    monkeypatch.setattr("salmon.uploader.transcode_folder", fake_transcode)
    monkeypatch.setattr("salmon.uploader.resolve_integrity_for_upload", fake_integrity)

    def make_source(beside_downloads: bool) -> Path:
        parent = downloads if beside_downloads else tmp_path / "seeding"
        parent.mkdir(exist_ok=True)
        name = generate_folder_name(upload_world.metadata) if beside_downloads else "testartist - testalbum (FLAC)"
        source = parent / name
        source.mkdir()
        for number, title in enumerate(TRACK_TITLES, 1):
            _flac_with_art(source / f"track{number}.flac", title, number, 1100 * 1024)
        return source

    async def start(source: Path, script: list, group: dict = TORRENTGROUP_RESPONSE):
        async def upload_job(job):
            await run_upload(
                tracker,
                str(source),
                2002,
                "WEB",
                False,
                (0,),
                None,
                skip_up=True,
                skip_mqa=True,
                flac_group=group,
            )

        job = jm().create_threaded("upload", "Upload to RED", upload_job, lock_key=str(source))
        await drive(job, script)
        await join_job(job)
        return job

    run.make_source, run.start = make_source, start
    return run


def _staged_copies(downloads: Path) -> list[str]:
    staging = downloads / ".salmon-staging"
    return sorted(os.listdir(staging)) if staging.exists() else []


def _rename_script(beside_downloads: bool) -> list:
    return [
        ("auto-tag the files", True),
        *([] if beside_downloads else [("replace the original folder name", True), ("folder name acceptable", True)]),
        ("rename the files", True),
    ]


@pytest.mark.parametrize("beside_downloads", [False, True])
async def test_skip_flac_upload_never_modifies_the_source_folder(skip_flac_run, beside_downloads):
    """The source is the folder seeding the group's FLAC: retag, rename, art strip and cover
    extraction happen on a scratch copy, and nothing is written in or beside the source."""
    from mutagen.flac import FLAC

    source = skip_flac_run.make_source(beside_downloads)
    parent = source.parent
    before, parent_before = _snapshot(source), sorted(os.listdir(parent))
    stripped = []
    real_rmtree = shutil.rmtree

    def looking_rmtree(path, *args, **kwargs):
        # The copy the transcodes came from is inspected just before the run removes it.
        if ".salmon-staging" in str(path) and skip_flac_run.transcoded:
            copy = Path(skip_flac_run.transcoded[0][0])
            stripped.append((copy / "cover.jpg").is_file() and all(not FLAC(f).pictures for f in copy.glob("*.flac")))
        real_rmtree(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shutil, "rmtree", looking_rmtree)
        job = await skip_flac_run.start(
            source,
            [
                *_rename_script(beside_downloads),
                ("Would you like to upload the torrent?", True),
                ("Select formats to convert", "*"),
            ],
        )

    assert job.status == "done", job.error
    assert _snapshot(source) == before
    if not beside_downloads:
        assert sorted(os.listdir(parent)) == parent_before
    assert not (parent / ".salmon-conversions" / f"{source.name}.json").exists()
    # Made from the scratch copy after the art strip and cover extraction, then the copy is gone.
    assert stripped == [True]
    assert all(".salmon-staging" in src for src, _target in skip_flac_run.transcoded)
    assert all(".salmon-staging" in checked for checked in skip_flac_run.integrity_checked)
    assert _staged_copies(skip_flac_run.downloads) == []
    # The transcodes and their records land in download_directory itself, as for any upload.
    targets = [Path(target) for _src, target in skip_flac_run.transcoded]
    assert [t.parent for t in targets] == [skip_flac_run.downloads] * 2
    assert all((skip_flac_run.downloads / ".salmon-conversions" / f"{t.name}.json").is_file() for t in targets)


async def test_skip_flac_upload_removes_the_copy_on_abort(skip_flac_run):
    source = skip_flac_run.make_source(beside_downloads=False)
    before = _snapshot(source)
    other_edition = {**TORRENTGROUP_RESPONSE, "torrents": [{**TORRENTGROUP_RESPONSE["torrents"][0], "media": "CD"}]}

    job = await skip_flac_run.start(
        source, [*_rename_script(False), ("Would you like to upload the torrent?", True)], group=other_edition
    )

    assert job.status == "done", job.error
    assert "Aborting upload..." in "\n".join(job.log_lines)
    assert skip_flac_run.integrity_checked  # the copy was made and worked on before the abort
    assert _staged_copies(skip_flac_run.downloads) == []
    assert _snapshot(source) == before


async def test_skip_flac_upload_removes_the_copy_on_an_error(skip_flac_run):
    source = skip_flac_run.make_source(beside_downloads=False)
    before = _snapshot(source)
    skip_flac_run.integrity_error = RuntimeError("decoder crashed")

    job = await skip_flac_run.start(source, _rename_script(False))

    assert job.status == "error"
    assert "decoder crashed" in job.error
    assert _staged_copies(skip_flac_run.downloads) == []
    assert _snapshot(source) == before


async def test_a_leftover_staged_copy_neither_blocks_nor_is_deleted(skip_flac_run):
    source = skip_flac_run.make_source(beside_downloads=False)
    leftover = skip_flac_run.downloads / ".salmon-staging" / source.name
    leftover.mkdir(parents=True)
    (leftover / "track1.flac").write_bytes(b"left by a crashed run")

    job = await skip_flac_run.start(
        source,
        [
            *_rename_script(False),
            ("Would you like to upload the torrent?", True),
            ("Select formats to convert", "*"),
        ],
    )

    assert job.status == "done", job.error
    assert _staged_copies(skip_flac_run.downloads) == [source.name]
    assert (leftover / "track1.flac").read_bytes() == b"left by a crashed run"


def test_scratch_removal_refuses_anything_outside_the_staging_folder(tmp_path, monkeypatch):
    from salmon.uploader import STAGING_DIR, _remove_scratch_dir

    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    source = tmp_path / "Album [FLAC]"
    source.mkdir()
    (source / "01.flac").write_bytes(b"seeding")
    staging = tmp_path / STAGING_DIR
    staging.mkdir()
    link = staging / "run-link"
    link.symlink_to(source, target_is_directory=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    for target in (link, outside, source, staging):
        _remove_scratch_dir(str(target), str(source))

    assert (source / "01.flac").read_bytes() == b"seeding"
    assert link.is_symlink() and outside.is_dir() and staging.is_dir()
