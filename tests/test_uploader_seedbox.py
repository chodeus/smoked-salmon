import subprocess

import anyio
import pytest

from salmon.config.validations import Seedbox
from salmon.uploader import seedbox


def test_rclone_upload_folder_reports_success(monkeypatch) -> None:
    run_process_calls: list[tuple[list[str], dict[str, object]]] = []
    messages: list[str] = []

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        run_process_calls.append((commands, kwargs))
        return subprocess.CompletedProcess(commands, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    ok = anyio.run(
        seedbox._rclone_upload_folder,
        Seedbox(url="seedbox", extra_args=["--checksum"]),
        "/music",
        "/tmp/Artist - Album",
    )

    assert ok is True
    assert run_process_calls == [
        (
            ["rclone", "copy", "/tmp/Artist - Album", "seedbox:/music/Artist - Album", "--checksum"],
            {"check": False},
        )
    ]
    assert any("Rclone upload successful" in message for message in messages)


def test_rclone_upload_folder_reports_the_failure_and_rclones_own_error(monkeypatch) -> None:
    messages: list[str] = []
    stderr = (
        b"2026/09/09 15:52:18 ERROR : 01. track.flac: Failed to copy: update stor: 1 error occurred:\n"
        b"\t* 426 Failure reading network stream.\n"
    )

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 7, stdout=b"", stderr=stderr)

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    ok = anyio.run(
        seedbox._rclone_upload_folder,
        Seedbox(url="seedbox"),
        "/music",
        "/tmp/Artist - Album",
    )

    assert ok is False
    assert "Rclone upload failed with exit code 7" in messages
    assert any("426 Failure reading network stream" in message for message in messages)


def _manager(monkeypatch, seedboxes):
    """UploadManager whose torrent clients are stubbed out (no network)."""
    monkeypatch.setattr(seedbox.cfg, "seedbox", seedboxes)
    monkeypatch.setattr(seedbox.click, "secho", lambda *a, **k: None)
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", lambda url: object())
    return seedbox.UploadManager()


def _sb(name, trackers, directory):
    return Seedbox(
        name=name,
        enabled=True,
        type="rclone",
        url="sbox",
        directory=directory,
        torrent_client="qbittorrent+http://u:p@host:10086/",
        trackers=trackers,
    )


def test_add_upload_task_routes_to_the_matching_tracker_only(monkeypatch) -> None:
    manager = _manager(monkeypatch, [_sb("red", ["RED"], "storage/red"), _sb("ops", ["OPS"], "storage/ops")])

    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="RED")

    assert [(sb.name, sb.directory) for sb, _, _ in manager.tasks] == [("red", "storage/red")]


def test_add_upload_task_sends_each_tracker_to_its_own_destination(monkeypatch) -> None:
    manager = _manager(monkeypatch, [_sb("red", ["RED"], "storage/red"), _sb("ops", ["OPS"], "storage/ops")])

    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="RED")
    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="OPS")

    assert sorted(sb.directory for sb, _, _ in manager.tasks) == ["storage/ops", "storage/red"]


def test_add_upload_task_without_trackers_still_matches_every_site(monkeypatch) -> None:
    # Back-compat: existing single-destination configs have no `trackers` key.
    manager = _manager(monkeypatch, [_sb("all", [], "storage/uploads")])

    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="OPS")
    manager.add_upload_task("/tmp/Artist - Album2", "folder", True, site_code="RED")

    assert len(manager.tasks) == 2


def test_add_upload_task_with_no_site_code_skips_pinned_seedboxes(monkeypatch) -> None:
    manager = _manager(monkeypatch, [_sb("red", ["RED"], "storage/red"), _sb("all", [], "storage/uploads")])

    manager.add_upload_task("/tmp/Artist - Album", "folder", True)

    assert [sb.name for sb, _, _ in manager.tasks] == ["all"]


def _run_folder_then_seed(monkeypatch, copy_ok: bool) -> tuple[list[bool], list[str]]:
    """Execute a folder task followed by its seed task; return the paused flags and log lines."""
    messages: list[str] = []
    paused_flags: list[bool] = []
    manager = _manager(monkeypatch, [_sb("red", ["RED"], "storage/red")])
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    async def fake_copy(_seedbox, _remote_folder, _path) -> bool:
        return copy_ok

    async def fake_add(_client, _shell_path, _torrent_path, _label, add_paused) -> bool:
        paused_flags.append(add_paused)
        return True

    monkeypatch.setattr(seedbox, "_rclone_upload_folder", fake_copy)
    monkeypatch.setattr(seedbox, "_add_to_downloader", fake_add)
    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="RED")
    manager.add_upload_task("/tmp/Artist - Album - RED.torrent", "seed", True, site_code="RED")
    anyio.run(manager.execute_upload)
    return paused_flags, messages


def test_seed_task_is_added_paused_when_its_folder_copy_failed(monkeypatch) -> None:
    paused_flags, messages = _run_folder_then_seed(monkeypatch, copy_ok=False)

    assert paused_flags == [True]
    assert any("adding the torrent paused" in message for message in messages)


def test_seed_task_stays_active_when_the_folder_copy_succeeded(monkeypatch) -> None:
    paused_flags, messages = _run_folder_then_seed(monkeypatch, copy_ok=True)

    assert paused_flags == [False]
    assert not any("paused" in message for message in messages)


def test_seedbox_trackers_are_uppercased() -> None:
    assert Seedbox(trackers=["red", "ops"]).trackers == ["RED", "OPS"]


def test_seedbox_rejects_unknown_tracker() -> None:
    with pytest.raises(ValueError, match="Unknown tracker"):
        Seedbox(name="typo", trackers=["REDD"])
