"""End-to-end tests for the web upload wizard.

Drives the UNMODIFIED salmon.uploader.upload() pipeline through
JobManager.create_threaded — the same entry point the /api/upload router
uses — where salmon.interaction puts every question to the browser, as a
question on the job.

The world is mocked at the narrowest seams of salmon/uploader/__init__.py
(metadata gathering, tag/rename plumbing, spectral *generation*, image
hosts) while the interactive machinery stays real: the source prompt, the
dupe checker and its prompts against a fake Gazelle API, check_spectrals'
lossy/selection prompts, the upload confirm, real torf torrent generation,
prepare_and_upload against the fake tracker, and the multi-tracker loop.

Tests answer questions exactly like the HTTP layer does: poll job.question
and deliver values via the job's asker (plus two genuine HTTP-level
tests for the answer endpoint and spectral image serving).
"""

import asyncio
import contextlib
import io
import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import asyncclick as click
import pytest
from torf import Torrent
from webui_client import reset_manager, web_client  # pyright: ignore[reportMissingImports]

import salmon.tagger.pre_data
import salmon.trackers
import salmon.uploader.spectrals
from salmon import cfg, interaction
from salmon.constants import RELEASE_TYPES
from salmon.uploader import upload as run_upload
from salmon.webui import jobs as jobs_mod
from salmon.webui import output
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
        answered = job.asker.answer(question["id"], answer)
        assert answered is True
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
# Fixtures: managers, fake tracker, mocked world
# ---------------------------------------------------------------------------


@pytest.fixture
def jm():
    """Factory for isolated JobManagers whose jobs print to their logs; cancels/joins leftover jobs on teardown."""
    managers = []
    with contextlib.ExitStack() as capture:

        def make() -> JobManager:
            if not managers:
                # As salmon web does while it serves; from the test itself, as pytest resets sys.stdout per phase.
                capture.enter_context(output.capturing())
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
        shown_spectrals=[],
    )

    monkeypatch.setattr("salmon.uploader.gather_audio_info", lambda path: dict(world.audio_info))
    monkeypatch.setattr("salmon.uploader.standardize_tags", lambda path: None)
    monkeypatch.setattr("salmon.uploader.gather_tags", lambda path: dict(world.tags))
    monkeypatch.setattr("salmon.uploader.check_embedded_pictures", lambda path: None)

    async def fake_check_tags(path):
        return dict(world.tags)

    async def fake_get_metadata(path, tags, rls_data, **_kwargs):
        return dict(world.metadata), None

    async def fake_review_metadata_with_ai(metadata, *args, **kwargs):
        return metadata

    async def fake_check_folder_structure(path, scene, essential_only=False, max_path_length=None):
        return None

    async def fake_download_cover(path, cover_url):
        return os.path.join(path, "cover.jpg"), False

    async def fake_upload_cover(cover_path, host=None, red_api=None):
        return "https://img.example/cover"

    async def fake_generate_spectrals_all(path, spectrals_path, audio_info):
        for idx in range(1, len(TRACK_FILES) + 1):
            for kind in ("Full", "Zoom"):
                (Path(spectrals_path) / f"{idx:02d} {kind}.png").write_bytes(b"png")
        return {i + 1: fn for i, fn in enumerate(TRACK_FILES)}

    async def fake_upload_spectrals(spectrals_path, spectral_ids, tracker=None):
        world.spectral_uploads.append((spectrals_path, dict(spectral_ids or {})))
        return {sid: [f"https://img.example/{sid}f", f"https://img.example/{sid}z"] for sid in (spectral_ids or {})}

    monkeypatch.setattr("salmon.uploader.check_tags", fake_check_tags)
    monkeypatch.setattr("salmon.uploader.get_metadata", fake_get_metadata)
    monkeypatch.setattr("salmon.uploader.review_metadata_with_ai", fake_review_metadata_with_ai)

    async def fake_tag_or_rename_files(*_args, **_kwargs):
        return None

    monkeypatch.setattr("salmon.uploader.tag_files", fake_tag_or_rename_files)
    monkeypatch.setattr("salmon.uploader.rename_files", fake_tag_or_rename_files)

    async def fake_rename_folder(path, metadata, auto_rename, parent=None, original=None):
        return path

    monkeypatch.setattr("salmon.uploader.rename_folder", fake_rename_folder)
    monkeypatch.setattr("salmon.uploader.check_folder_structure", fake_check_folder_structure)
    monkeypatch.setattr("salmon.uploader.download_cover_if_nonexistent", fake_download_cover)
    monkeypatch.setattr("salmon.uploader.upload_cover", fake_upload_cover)
    monkeypatch.setattr("salmon.uploader.spectrals.generate_spectrals_all", fake_generate_spectrals_all)
    monkeypatch.setattr("salmon.uploader.spectrals.upload_spectrals", fake_upload_spectrals)
    show_spectrals = jobs_mod._JobAsker.show_spectrals

    def recording_show_spectrals(asker, folder, files):
        show_spectrals(asker, folder, files)
        world.shown_spectrals.append(asker.spectrals)

    monkeypatch.setattr(jobs_mod._JobAsker, "show_spectrals", recording_show_spectrals)
    # The fake spectrals are not PNGs: compressing the picked ones would fail on them.
    monkeypatch.setattr(cfg.upload.compression, "compress_spectrals", False)

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
    monkeypatch.setattr(cfg.image, "embed_missing_cover", False)
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
            ("Look at the spectrals, then go on.", True),
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

    assert [q["kind"] for q in questions] == [
        "prompt",
        "prompt",
        "spectrals",
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

    # Spectrals surfaced to the browser instead of a native viewer, the selected ids reached the (stubbed)
    # spectral uploader, and the folder deleted once they were up is no longer shown.
    shown = upload_world.shown_spectrals[-1]
    assert len(shown["files"]) == 6
    assert upload_world.spectral_uploads == [(shown["path"], {1: TRACK_FILES[0], 2: TRACK_FILES[1], 3: TRACK_FILES[2]})]
    assert job.asker.spectrals is None

    log = "\n".join(job.log_lines)
    assert "Uploading to a new torrent group." in log
    assert "Successfully uploaded https://redacted.sh/torrents.php?torrentid=1001" in log
    assert "Done with this release." in log

    # Everything flowed to subscribers (what the jobs websocket relays).
    await asyncio.sleep(0.05)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    kinds = [e["event"] for e in events]
    assert kinds.count("question") == 8
    assert kinds.count("question_answered") == 8
    assert "spectrals_ready" in kinds
    assert "log" in kinds
    assert kinds[0] == "created"
    assert kinds[-1] == "finished"


async def test_a_job_without_a_trackers_list_offers_no_other_without_multi_tracker_upload(
    jm, tracker, album_dir, upload_world, monkeypatch
):
    # Such a job passes only its tracker: only multi_tracker_upload lets the run go on to another.
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", False)
    m = jm()
    path = str(album_dir)
    job = m.create_threaded("upload", "Upload to RED", wizard_factory(tracker, path), lock_key=path)

    await drive(
        job,
        [
            ("What is the source of this release?", "web"),
            ("Would you like to upload to an existing group?", "n"),
            ("Look at the spectrals, then go on.", True),
            ("Is this release lossy mastered?", "n"),
            ("What spectral IDs would you like to upload", "*"),
            ("Would you like to upload the torrent?", True),
            ("Would you like to check downconversion options?", False),
        ],
    )
    await join_job(job)

    assert job.status == "done", job.error
    assert len(tracker.uploads) == 1
    assert "Done uploading this release." in "\n".join(job.log_lines)


# ---------------------------------------------------------------------------
# 2. Abort paths
# ---------------------------------------------------------------------------


async def test_abort_at_source_prompt_errors_job_and_releases_lock(jm, tracker, album_dir, upload_world):
    m = jm()
    path = str(album_dir)
    job = m.create_threaded("upload", "Upload", wizard_factory(tracker, path), lock_key=path)

    await drive(job, [("What is the source of this release?", "a")])
    await join_job(job)

    assert job.status == "failed"
    assert job.error == "Aborted."
    assert job.result is None
    assert job.question is None
    assert tracker.uploads == []
    assert path not in m._active_lock_keys


async def test_abort_at_dupe_choice_is_swallowed_by_the_pipeline(jm, tracker, album_dir, upload_world):
    """Deviation from the wishlist: upload() catches click.Abort raised at the
    dupe-choice prompt internally and returns after logging "Aborting
    upload..." — so at job level this is a clean 'done', not an error. The
    'Aborted.' failed status is only reachable from prompts outside upload()'s
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

    calls: list[str | None] = []

    async def cover_fails_on_red(cover_path, host=None, red_api=None):
        # RED's upload comes first and fails; OPS shares the host, so its cover is uploaded again.
        calls.append(host)
        return None if len(calls) == 1 else "https://img.example/ops-cover"

    monkeypatch.setattr("salmon.uploader.upload_cover", cover_fails_on_red)

    m = jm()
    path = str(album_dir)
    job = m.create_threaded("upload", "Upload to RED", wizard_factory(tracker, path), lock_key=path)

    await drive(
        job,
        [
            ("What is the source of this release?", "web"),
            ("Would you like to upload to an existing group?", "n"),
            ("Look at the spectrals, then go on.", True),
            ("Is this release lossy mastered?", "n"),
            ("What spectral IDs would you like to upload", "*"),
            ("Would you like to upload the torrent?", True),
            ("Continue without a cover image?", "n"),
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
        return await interaction.prompt("The only question")

    job = m.create_threaded("demo", "Demo", factory)
    question = await wait_for(lambda: job.question, "question to appear")

    answered = m.answer("job-does-not-exist", question["id"], "x")
    assert answered is False
    answered = job.asker.answer("q-bogus", "x")
    assert answered is False
    assert job.question is not None  # still pending after bad answers
    answered = job.asker.answer(question["id"], "the answer")
    assert answered is True

    await join_job(job)
    assert job.status == "done"
    assert job.result == "the answer"
    # Stale answers after the question was consumed are rejected too.
    answered = job.asker.answer(question["id"], "late")
    assert answered is False


async def test_confirm_questions_accept_boolean_and_string_answers(jm):
    m = jm()

    async def factory(job):
        return [
            await interaction.confirm("Boolean yes?"),
            await interaction.confirm("String yes?"),
            await interaction.confirm("Default yes?", default=True),
            await interaction.confirm("String no?"),
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


async def test_a_prompt_is_asked_again_with_the_error_and_takes_its_default(jm):
    m = jm()

    async def factory(job):
        return [
            await interaction.prompt("How many discs?", type=int),
            await interaction.prompt("Which?", type=click.Choice(["one", "two"])),
            await interaction.prompt("Label", default="none"),
        ]

    job = m.create_threaded("demo", "Demo", factory)
    questions = await drive(
        job,
        [("How many discs?", "many"), ("How many discs?", "2"), ("Which?", "three"), ("Which?", "two"), ("Label", "")],
    )
    await join_job(job)

    assert job.status == "done", job.error
    assert job.result == [2, "two", "none"]
    assert (questions[0]["kind"], questions[0]["error"]) == ("prompt", None)
    assert questions[1]["error"] == "Error: 'many' is not a valid integer."
    assert questions[2]["choices"] == ["one", "two"]
    assert questions[4]["default"] == "none"
    # As the terminal does, the error is printed too.
    assert "Error: 'many' is not a valid integer." in job.log_lines


async def test_a_confirm_without_a_default_is_asked_again_on_an_empty_answer(jm):
    m = jm()

    async def factory(job):
        return await interaction.confirm("Third?", default=None)

    job = m.create_threaded("demo", "Demo", factory)
    questions = await drive(job, [("Third?", ""), ("Third?", "N")])
    await join_job(job)

    assert job.result is False
    assert (questions[0]["default"], questions[1]["error"]) == (None, "Error: invalid input")


async def test_spectrals_whose_folder_the_job_deleted_are_no_longer_shown(jm, tmp_path):
    m = jm()
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "01-full.png").write_bytes(b"PNG")
    queue = m.subscribe()

    async def factory(job):
        await interaction.show_spectrals(str(specs), {1: TRACK_FILES[0]})
        shown = job.to_dict()["spectrals"]
        shutil.rmtree(specs)
        await interaction.confirm("Upload?")
        return shown, job.to_dict()["spectrals"]

    job = m.create_threaded("demo", "Demo", factory)
    await drive(job, [("Look at the spectrals", True), ("Upload?", True)])
    await join_job(job)

    # Gone by the next question, before the job ends.
    assert job.result == (["01-full.png"], None)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert [e["files"] for e in events if e["event"] == "spectrals_ready"] == [["01-full.png"], []]


async def test_a_job_that_goes_on_after_its_cancel_is_stopped_at_its_next_question(jm):
    m = jm()

    async def factory(job):
        # As a job whose cancel came back as an unknown outcome goes on.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(30)
        return await interaction.confirm("Still upload?")

    job = m.create_threaded("demo", "Demo", factory)
    await wait_for(lambda: job._thread_task is not None, "the job's task to start")
    m.cancel(job.id)
    await join_job(job)

    assert (job.status, job.question, job.result) == ("cancelled", None, None)


async def test_a_web_job_shows_the_spectrals_the_terminal_would(jm, tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "01 Full.png").write_bytes(b"PNG")
    # A compression's leftover copy, which the terminal's viewers leave out.
    (specs / ".01 Full.png.tmp.png").write_bytes(b"PNG")
    m = jm()

    async def factory(job):
        await interaction.show_spectrals(str(specs), {1: TRACK_FILES[0]})

    job = m.create_threaded("demo", "Demo", factory)
    questions = await drive(job, [("Look at the spectrals", True)])
    await join_job(job)

    assert questions[0]["files"] == salmon.uploader.spectrals._spectral_images(str(specs)) == ["01 Full.png"]


async def test_spectrals_deleted_after_the_last_question_are_gone_once_the_job_ends(jm, tmp_path):
    m = jm()
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "01-full.png").write_bytes(b"PNG")

    async def factory(job):
        await interaction.show_spectrals(str(specs), {1: TRACK_FILES[0]})
        shutil.rmtree(specs)

    job = m.create_threaded("demo", "Demo", factory)
    await drive(job, [("Look at the spectrals", True)])
    await join_job(job)

    assert job.to_dict()["spectrals"] is None


def test_spectrals_shown_while_the_old_ones_are_withdrawn_stay_shown(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    events: list[dict] = []
    asker = jobs_mod._JobAsker(emit=events.append, set_question=lambda _question: None)
    asker.show_spectrals(str(old), ["01 Full.png"])
    old.rmdir()
    isdir = os.path.isdir
    showers: list[threading.Thread] = []

    def isdir_while_new_ones_are_shown(path) -> bool:
        if not showers:
            # The job's thread shows new spectrals while an image request withdraws the old ones.
            showers.append(threading.Thread(target=asker.show_spectrals, args=(str(new), ["02 Full.png"])))
            showers[0].start()
            showers[0].join(0.2)
        return isdir(path)

    monkeypatch.setattr(jobs_mod.os.path, "isdir", isdir_while_new_ones_are_shown)
    asker.withdraw_deleted_spectrals()
    showers[0].join(5)

    assert asker.spectrals is not None and list(asker.spectrals["files"]) == ["02 Full.png"]
    assert events[-1] == {"event": "spectrals_ready", "files": ["02 Full.png"]}


# ---------------------------------------------------------------------------
# 4. Cancel while a question is pending
# ---------------------------------------------------------------------------


async def test_cancel_while_a_prompt_is_pending_cancels_job_and_frees_lock(jm):
    m = jm()

    async def factory(job):
        return await interaction.prompt("Never answered?")

    job = m.create_threaded("demo", "Demo", factory, lock_key="/albums/x")
    await wait_for(lambda: job.question, "question to appear")

    cancelled = m.cancel(job.id)
    assert cancelled is True
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


@pytest.mark.parametrize("asked", ["prompt", "confirm"])
async def test_an_unanswered_question_stops_the_job_and_frees_its_lock(jm, monkeypatch, asked):
    # Upstream's rule: a question left unanswered for QUESTION_TIMEOUT stops its job, which frees the folder.
    monkeypatch.setattr(jobs_mod, "QUESTION_TIMEOUT", 0.3)
    m = jm()

    async def factory(job):
        if asked == "prompt":
            return await interaction.prompt("Never answered?")
        return await interaction.confirm("Never answered?")

    job = m.create_threaded("demo", "Demo", factory, lock_key="/albums/z")
    await join_job(job)

    assert job.status == "failed"
    assert job.error == jobs_mod._no_answer()
    assert job.question is None
    assert "/albums/z" not in m._active_lock_keys


async def test_cancel_while_a_confirm_is_pending_clears_question_and_lock(jm):
    """A question's future is a concurrent.futures one: its CancelledError still ends the job as cancelled."""
    m = jm()

    async def factory(job):
        await interaction.confirm("Never answered?")
        return "unreachable"

    job = m.create_threaded("demo", "Demo", factory, lock_key="/albums/y")
    await wait_for(lambda: job.question, "question to appear")

    cancelled = m.cancel(job.id)
    await join_job(job)

    assert cancelled is True
    assert job.status == "cancelled"
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
        return {"alpha": await interaction.prompt("Alpha question")}

    async def beta(job):
        return {"beta": await interaction.prompt("Beta question")}

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
    answered = job_b.asker.answer(job_a.question["id"], "x")
    assert answered is False

    answered = job_a.asker.answer(job_a.question["id"], "from a")
    assert answered is True
    answered = job_b.asker.answer(job_b.question["id"], "from b")
    assert answered is True
    await join_job(job_a)
    await join_job(job_b)

    assert job_a.result == {"alpha": "from a"}
    assert job_b.result == {"beta": "from b"}
    assert m._active_lock_keys == set()


# ---------------------------------------------------------------------------
# 6. Log capture inside jobs; CLI context untouched
# ---------------------------------------------------------------------------


async def test_secho_goes_to_the_job_log_only_and_the_cli_prints_as_ever():
    async def factory(job):
        click.secho("streamed to the browser", fg="cyan")
        return "ok"

    out = io.StringIO()
    with contextlib.redirect_stdout(out), output.capturing():
        job = JobManager().create_threaded("demo", "Demo", factory)
        await join_job(job)
        # Outside any job (CLI context): printed as ever, and no job log.
        click.secho("plain cli line")

    assert job.log_lines == ["streamed to the browser"]
    assert out.getvalue() == "plain cli line\n"


# ---------------------------------------------------------------------------
# 7. An edit, and the encoding prompt (pre_data)
# ---------------------------------------------------------------------------


async def test_edit_and_encoding_prompt_seams(jm):
    m = jm()

    async def factory(job):
        edited = await interaction.edit("original text")
        kept = await interaction.edit("keep me")
        encoding = await salmon.tagger.pre_data._prompt_encoding()
        return {"edited": edited, "kept": kept, "encoding": encoding}

    job = m.create_threaded("demo", "Demo", factory)
    questions = await drive(
        job,
        [
            ("Edit the text, then save it.", "rewritten body"),
            ("Edit the text, then save it.", None),  # discard -> None
            ("What is the encoding of this release?", "320"),
        ],
    )
    await join_job(job)

    assert job.status == "done", job.error
    assert [q["kind"] for q in questions] == ["edit", "edit", "prompt"]
    assert questions[0]["initial"] == "original text"
    assert questions[1]["initial"] == "keep me"
    assert job.result == {"edited": "rewritten body", "kept": None, "encoding": ["320", False]}


# ---------------------------------------------------------------------------
# HTTP level: answer endpoint + spectral image serving for a threaded job
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    reset_manager()
    with web_client() as c:
        try:
            yield c
        finally:
            for job in list(global_manager.jobs.values()):
                if job.finished_at is None:
                    global_manager.cancel(job.id)
            for job in global_manager.jobs.values():
                if job.thread is not None:
                    job.thread.join(timeout=5)
    reset_manager()


def test_http_answer_endpoint_and_spectral_serving(client, tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "01-full.png").write_bytes(b"PNG-FULL")
    (specs / "02-zoom.png").write_bytes(b"PNG-ZOOM")

    async def factory(job):
        await interaction.show_spectrals(str(specs), {1: TRACK_FILES[0]})
        return {"confirmed": await interaction.confirm("Ship it to the tracker?")}

    assert client.portal is not None
    job = client.portal.call(
        lambda: global_manager.create_threaded("upload", "Wizard", factory, lock_key="/albums/http")
    )

    looked = wait_until(lambda: job.question, "the spectrals to be shown")

    data = client.get(f"/api/jobs/{job.id}").json()
    assert data["question"]["kind"] == "spectrals"
    assert data["question"]["files"] == ["01-full.png", "02-zoom.png"]
    assert data["question"]["tracks"] == {"01": TRACK_FILES[0]}
    assert data["spectrals"] == ["01-full.png", "02-zoom.png"]

    resp = client.get(f"/api/jobs/{job.id}/spectrals/01-full.png")
    assert resp.status_code == 200
    assert resp.content == b"PNG-FULL"
    assert resp.headers["content-type"] == "image/png"
    refused = client.get(f"/api/jobs/{job.id}/spectrals/nope.png")
    assert refused.status_code == 404
    refused = client.get("/api/jobs/job-unknown/spectrals/01-full.png")
    assert refused.status_code == 404

    done = client.post(f"/api/jobs/{job.id}/answer", json={"question_id": looked["id"], "value": True})
    assert done.status_code == 200
    question = wait_until(
        lambda: job.question if job.question and job.question["id"] != looked["id"] else None, "the next question"
    )
    assert question["text"] == "Ship it to the tracker?"
    assert question["kind"] == "confirm"

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


def test_spectrals_the_job_deleted_are_not_found_and_no_longer_shown(client, tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "01 Full.png").write_bytes(b"PNG")
    deleted, uploaded = threading.Event(), threading.Event()

    async def factory(job):
        await interaction.show_spectrals(str(specs), {1: TRACK_FILES[0]})
        # As handle_spectrals_upload_and_deletion does, before the tracker upload and any next question.
        shutil.rmtree(specs)
        deleted.set()
        await asyncio.to_thread(uploaded.wait, 10)

    assert client.portal is not None
    job = client.portal.call(lambda: global_manager.create_threaded("upload", "Wizard", factory))
    looked = wait_until(lambda: job.question, "the spectrals question")
    answered = client.post(f"/api/jobs/{job.id}/answer", json={"question_id": looked["id"], "value": True})
    assert answered.status_code == 200
    wait_until(deleted.is_set, "the spectrals folder to be deleted")

    image = client.get(f"/api/jobs/{job.id}/spectrals/01%20Full.png")
    listed = client.get(f"/api/jobs/{job.id}").json()["spectrals"]
    uploaded.set()

    assert (image.status_code, listed) == (404, None)


def test_a_spectral_image_swapped_for_a_link_is_not_served(client, tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    image = specs / "01 Full.png"
    image.write_bytes(b"PNG")
    elsewhere = tmp_path / "elsewhere.png"
    elsewhere.write_bytes(b"not a spectral")

    async def factory(job):
        await interaction.show_spectrals(str(specs), {1: TRACK_FILES[0]})

    assert client.portal is not None
    job = client.portal.call(lambda: global_manager.create_threaded("upload", "Wizard", factory))
    looked = wait_until(lambda: job.question, "the spectrals question")
    served = client.get(f"/api/jobs/{job.id}/spectrals/01%20Full.png")
    image.unlink()
    image.symlink_to(elsewhere)
    linked = client.get(f"/api/jobs/{job.id}/spectrals/01%20Full.png")
    answered = client.post(f"/api/jobs/{job.id}/answer", json={"question_id": looked["id"], "value": True})
    wait_until(lambda: job.finished_at is not None, "the job to finish")

    assert (served.status_code, served.content) == (200, b"PNG")
    assert linked.status_code == 404
    assert answered.status_code == 200


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
            ("Look at the spectrals, then go on.", True),
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
    import io

    from PIL import Image

    jpeg = io.BytesIO()
    Image.new("RGB", (4, 4)).save(jpeg, "jpeg")
    # A real JPEG, padded after its end marker to the size the test needs.
    picture.type, picture.mime, picture.data = PictureType.COVER_FRONT, "image/jpeg", jpeg.getvalue() + bytes(art_bytes)
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
    from salmon.tagger import audio_info, cover, retagger, tags
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
        "check_embedded_pictures": cover.check_embedded_pictures,
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
    """Retag, rename, art strip and cover extraction hit a scratch copy; nothing is written in or beside the source."""
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

    assert job.status == "failed"
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
    from salmon.uploader.staging import STAGING_DIR, _remove_scratch_dir

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


async def test_an_unanswered_question_inside_the_upload_stops_the_job_as_unanswered(
    jm, tracker, album_dir, upload_world, monkeypatch
):
    # salmon up catches its own aborts; one from an unanswered web question must still reach the job.
    monkeypatch.setattr(jobs_mod, "QUESTION_TIMEOUT", 1.0)
    m = jm()
    path = str(album_dir)
    job = m.create_threaded("upload", "Upload", wizard_factory(tracker, path), lock_key=path)
    await wait_for(lambda: job.question and "source" in job.question["text"], "the source question")
    answered = job.asker.answer(job.question["id"], "web")
    assert answered is True

    # The next question (the existing group) is left unanswered.
    await join_job(job)

    assert (job.status, job.error) == ("failed", jobs_mod._no_answer())
    assert path not in m._active_lock_keys
