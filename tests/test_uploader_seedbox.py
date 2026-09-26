import subprocess

import anyio
import pytest

from salmon.common.redaction import redact_command, redact_secrets, secret_values
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


def test_rclone_upload_folder_treats_a_launch_failure_as_a_failed_copy(monkeypatch) -> None:
    messages: list[str] = []

    async def missing_binary(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError(2, "No such file or directory", "rclone")

    monkeypatch.setattr(seedbox.anyio, "run_process", missing_binary)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    ok = anyio.run(seedbox._rclone_upload_folder, Seedbox(url="seedbox"), "/music", "/tmp/Artist - Album")

    assert ok is False
    assert any("rclone could not start" in message for message in messages)


def test_rclone_command_and_output_are_redacted_before_they_reach_the_log(monkeypatch) -> None:
    messages: list[str] = []
    stderr = (
        b"2026/09/09 15:52:18 ERROR : ftp://dean:hunter2@box.example/music: 530 Login incorrect\n"
        b"authentication failed for hunter2\n"
    )

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 1, stdout=b"", stderr=stderr)

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    ok = anyio.run(
        seedbox._rclone_upload_folder,
        Seedbox(
            url="seedbox",
            extra_args=[
                "--ftp-pass",
                "hunter2",
                "--sftp-pass=hunter2",
                "--sftp-key-pem",
                "-----BEGIN KEY----- hunter2 -----END KEY-----",
                "--ftp-pass",
                "it's hunter2",
                "--sftp-key-pem=BEGIN KEY hunter2 DATA",
            ],
        ),
        "/music",
        "/tmp/Artist - Album",
    )

    assert ok is False
    assert not any("hunter2" in message for message in messages)
    assert any("[REDACTED]" in message for message in messages)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("key_pem=hunter2", "key_pem=[REDACTED]"),
        ("--session hunter2", "--session [REDACTED]"),
        ("--sftp-key-pem=hunter2", "--sftp-key-pem=[REDACTED]"),
        ("token=hunter2", "token=[REDACTED]"),
        ("sftp://dean:hunter2@box/x", "sftp://[REDACTED]@box/x"),
        ("copy /music sbox:storage/red --transfers 4", "copy /music sbox:storage/red --transfers 4"),
        # rclone connection strings may quote a value that has spaces in it, such as a PEM key.
        (":sftp,key_pem='-----BEGIN KEY----- hunter2 -----END KEY-----',user=x:", ":sftp,key_pem=[REDACTED],user=x:"),
        ('--sftp-key-pem "-----BEGIN KEY----- hunter2"', "--sftp-key-pem [REDACTED]"),
        # rclone doubles a quote inside a quoted value.
        (":sftp,pass='hunter''2',user=x:", ":sftp,pass=[REDACTED],user=x:"),
    ],
)
def test_redact_masks_suffixed_option_names_and_sessions(text: str, expected: str) -> None:
    assert seedbox.redact_secrets(text) == expected


def test_a_credential_bearing_remote_never_reaches_the_log(monkeypatch) -> None:
    # rclone accepts connection strings as remotes, so the "url" itself can carry a password.
    messages: list[str] = []

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    ok = anyio.run(
        seedbox._rclone_upload_folder,
        Seedbox(url=":ftp,host=box.example,user=dean,pass=hunter2"),
        "/music",
        "/tmp/Artist - Album",
    )

    shown = ":ftp,host=box.example,user=dean,pass=[REDACTED]"
    assert ok is True
    assert messages[0] == f"Starting Rclone upload to {shown}"
    assert messages[1].startswith(f"Executing: rclone copy '/tmp/Artist - Album' '{shown}")
    assert messages[2].startswith(f"Rclone upload successful: /tmp/Artist - Album to {shown}")
    assert not any("hunter2" in message for message in messages)


def _manager(monkeypatch, seedboxes):
    """UploadManager whose torrent clients are stubbed out (no network)."""
    monkeypatch.setattr(seedbox.cfg, "seedbox", seedboxes)
    monkeypatch.setattr(seedbox.click, "secho", lambda *a, **k: None)
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", lambda url: object())
    return seedbox.UploadManager()


def _sb(name, trackers, directory, url="sbox"):
    return Seedbox(
        name=name,
        enabled=True,
        type="rclone",
        url=url,
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


def test_a_failed_copy_pauses_only_its_own_seedbox_even_when_names_repeat(monkeypatch) -> None:
    # Two destinations with the default empty name: only the one whose copy failed is paused.
    manager = _manager(
        monkeypatch, [_sb("", ["RED"], "storage/a", url="boxa"), _sb("", ["RED"], "storage/b", url="boxb")]
    )
    paused: dict[str, bool] = {}

    async def fake_copy(sb, _remote_folder, _path) -> bool:
        return sb.url != "boxa"

    async def fake_add(_client, shell_path, _torrent_path, _label, add_paused) -> bool:
        paused[shell_path] = add_paused
        return True

    monkeypatch.setattr(seedbox, "_rclone_upload_folder", fake_copy)
    monkeypatch.setattr(seedbox, "_add_to_downloader", fake_add)
    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="RED")
    manager.add_upload_task("/tmp/Artist - Album - RED.torrent", "seed", True, site_code="RED")
    anyio.run(manager.execute_upload)

    assert {path.split("/")[-1]: flag for path, flag in paused.items()} == {"a": True, "b": False}


def test_seedbox_trackers_are_uppercased() -> None:
    assert Seedbox(trackers=["red", "ops"]).trackers == ["RED", "OPS"]


def test_seedbox_rejects_unknown_tracker() -> None:
    with pytest.raises(ValueError, match="Unknown tracker"):
        Seedbox(name="typo", trackers=["REDD"])


def test_redact_command_masks_an_equals_form_secret_whole() -> None:
    shown = redact_command(["rclone", "copy", "a", "b", "--sftp-key-pem=BEGIN KEY PRIVATEPART DATA"])
    assert "PRIVATEPART" not in shown
    assert "--sftp-key-pem=[REDACTED]" in shown


def test_redact_command_shows_only_allowlisted_flag_values() -> None:
    shown = redact_command(
        ["rclone", "copy", "a", "b", "--transfers", "4", "--http-headers", '"Authorization","hunter2"', "--bwlimit=8M"]
    )
    assert "--transfers 4" in shown
    assert "--bwlimit=8M" in shown
    assert "hunter2" not in shown
    assert "Authorization" not in shown


def test_an_echoed_header_credential_never_reaches_the_log(monkeypatch) -> None:
    messages: list[str] = []

    async def fake_run_process(commands: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(commands, 1, stdout=b"", stderr=b"401 for Authorization: hunter2\n")

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)
    monkeypatch.setattr(seedbox.click, "secho", lambda message, **kwargs: messages.append(message))

    anyio.run(
        seedbox._rclone_upload_folder,
        Seedbox(url="web", extra_args=["--http-headers", "Authorization,hunter2"]),
        "/music",
        "/tmp/Album",
    )

    assert any(message.startswith("  rclone:") and "401 for" in message for message in messages)
    assert not any("hunter2" in message for message in messages)


def test_a_short_known_secret_is_masked_as_a_whole_word() -> None:
    assert redact_secrets("authentication failed for ab", known=["ab"]) == "authentication failed for [REDACTED]"
    assert redact_secrets("about tabs", known=["ab"]) == "about tabs"


def test_a_pem_value_starting_with_dashes_is_masked_and_collected() -> None:
    pem = "-----BEGIN OPENSSH PRIVATE KEY----- UNIQUEKEYMATERIAL -----END OPENSSH PRIVATE KEY-----"
    args = ["rclone", "copy", "a", "b", "--sftp-key-pem", pem]
    assert "UNIQUEKEYMATERIAL" not in redact_command(args)
    assert pem in secret_values(args)


def test_a_dumped_auth_header_is_masked() -> None:
    dumped = "2026/09/26 DEBUG : HTTP REQUEST\nAuthorization: Bearer UNIQUETOKEN\nUser-Agent: rclone/v1.72.0"
    shown = redact_secrets(dumped)
    assert "UNIQUETOKEN" not in shown
    assert "User-Agent: rclone/v1.72.0" in shown


def test_a_secret_value_that_looks_like_a_flag_is_still_masked() -> None:
    args = ["rclone", "copy", "a", "b", "--sftp-pass", "-UNIQUEMATERIAL", "--progress", "--transfers", "4"]
    shown = redact_command(args)
    assert "UNIQUEMATERIAL" not in shown
    # A known switch takes no value, so what follows it is shown as usual.
    assert "--progress --transfers 4" in shown
    assert "-UNIQUEMATERIAL" in secret_values(args)
