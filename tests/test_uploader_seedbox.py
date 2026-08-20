import subprocess

import anyio
import pytest

from salmon.config.validations import Seedbox
from salmon.uploader import seedbox


def test_rclone_upload_folder_streams_progress_output(monkeypatch) -> None:
    run_process_calls: list[tuple[list[str], dict[str, object]]] = []
    messages: list[str] = []

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        run_process_calls.append((commands, kwargs))
        return subprocess.CompletedProcess(commands, 0)

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    anyio.run(
        seedbox._rclone_upload_folder,
        Seedbox(url="seedbox", extra_args=["--checksum", "-P"]),
        "/music",
        "/tmp/Artist - Album",
    )

    assert run_process_calls == [
        (
            ["rclone", "copy", "/tmp/Artist - Album", "seedbox:/music/Artist - Album", "--checksum", "-P"],
            {"stdout": None, "stderr": None, "check": False},
        )
    ]
    assert any("Rclone upload successful" in message for message in messages)


def test_rclone_upload_folder_reports_nonzero_exit_code(monkeypatch) -> None:
    messages: list[str] = []

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 7)

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    anyio.run(
        seedbox._rclone_upload_folder,
        Seedbox(url="seedbox", extra_args=["-P"]),
        "/music",
        "/tmp/Artist - Album",
    )

    assert "Rclone upload failed with exit code 7" in messages


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


def test_seedbox_trackers_are_uppercased() -> None:
    assert Seedbox(trackers=["red", "ops"]).trackers == ["RED", "OPS"]


def test_seedbox_rejects_unknown_tracker() -> None:
    with pytest.raises(ValueError, match="Unknown tracker"):
        Seedbox(name="typo", trackers=["REDD"])
