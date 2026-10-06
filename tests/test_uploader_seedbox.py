import os
import subprocess
import sys

import anyio
import msgspec
import pytest

from salmon.common.redaction import redact_command, redact_secrets, secret_values
from salmon.config.validations import Seedbox
from salmon.uploader import seedbox
from salmon.uploader.torrent_client import DelugeClient, QBittorrentClient, TransmissionClient


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

    assert [(sb.name, sb.directory) for sb, *_ in manager.tasks] == [("red", "storage/red")]


def test_add_upload_task_sends_each_tracker_to_its_own_destination(monkeypatch) -> None:
    manager = _manager(monkeypatch, [_sb("red", ["RED"], "storage/red"), _sb("ops", ["OPS"], "storage/ops")])

    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="RED")
    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="OPS")

    assert sorted(sb.directory for sb, *_ in manager.tasks) == ["storage/ops", "storage/red"]


def test_add_upload_task_without_trackers_still_matches_every_site(monkeypatch) -> None:
    # Back-compat: existing single-destination configs have no `trackers` key.
    manager = _manager(monkeypatch, [_sb("all", [], "storage/uploads")])

    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="OPS")
    manager.add_upload_task("/tmp/Artist - Album2", "folder", True, site_code="RED")

    assert len(manager.tasks) == 2


def test_add_upload_task_with_no_site_code_skips_pinned_seedboxes(monkeypatch) -> None:
    manager = _manager(monkeypatch, [_sb("red", ["RED"], "storage/red"), _sb("all", [], "storage/uploads")])

    manager.add_upload_task("/tmp/Artist - Album", "folder", True)

    assert [sb.name for sb, *_ in manager.tasks] == ["all"]


def test_a_failed_copy_skips_only_its_own_seedbox_even_when_names_repeat(monkeypatch) -> None:
    # Two destinations with the default empty name: only the one whose copy failed skips its seed.
    manager = _manager(
        monkeypatch, [_sb("", ["RED"], "storage/a", url="boxa"), _sb("", ["RED"], "storage/b", url="boxb")]
    )
    seeded: list[str] = []

    async def fake_copy(sb, _remote_folder, _path) -> bool:
        return sb.url != "boxa"

    async def fake_add(_client, shell_path, *_args) -> bool:
        seeded.append(shell_path)
        return True

    monkeypatch.setattr(seedbox, "_rclone_upload_folder", fake_copy)
    monkeypatch.setattr(seedbox, "_add_to_downloader", fake_add)
    manager.add_upload_task("/tmp/Artist - Album", "folder", True, site_code="RED")
    manager.add_upload_task(
        "/tmp/Artist - Album - RED.torrent", "seed", True, folder="/tmp/Artist - Album", site_code="RED"
    )
    anyio.run(manager.execute_upload)

    assert seeded == ["storage/b"]


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


@pytest.mark.parametrize(
    ("args", "remote", "secret"),
    [
        ([], ":http,headers='Authorization,UNIQUETOKEN':", "UNIQUETOKEN"),
        ([], ":http,url='https://user:UNIQUEMATERIAL@example.com':", "UNIQUEMATERIAL"),
        (["--http-url", "https://user:UNIQUEARGPASS@example.com"], "web", "UNIQUEARGPASS"),
    ],
    ids=["connection-string header list", "connection-string url", "url argument"],
)
def test_embedded_credentials_are_collected_for_echoed_errors(args, remote, secret) -> None:
    assert secret in secret_values(args, remote)
    assert secret not in redact_secrets(f"401 for {secret}", secret_values(args, remote))


# Ported from upstream (smokin-salmon/smoked-salmon), adapted to the fork's opt-in seedboxes and captured rclone.


def _box(**fields) -> Seedbox:
    return Seedbox(**{"enabled": True, **fields})


def _route_rclone(monkeypatch, run) -> None:
    """Fake rclone with run(commands, secrets) -> exit code, the seam upstream's tests use."""

    async def fake_run_process(commands, **_kwargs):
        return subprocess.CompletedProcess(commands, await run(commands, []), stdout=b"", stderr=b"")

    monkeypatch.setattr(seedbox.anyio, "run_process", fake_run_process)


async def _async_true() -> bool:
    return True


def _fake_rclone(
    monkeypatch, tmp_path, *, stdout: str = "", stderr: str = "", exit_code: int = 0, code: str = ""
) -> None:
    """Put an `rclone` first on PATH that runs `code`, prints the given text and exits with exit_code."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "rclone"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        f"{code}\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _upload_with_fake_rclone(seedbox_config: Seedbox) -> bool:
    return anyio.run(seedbox._rclone_upload_folder, seedbox_config, "/music", "/tmp/Artist - Album")


needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="the fake rclone is a script with a shebang")


@needs_posix
@pytest.mark.parametrize(
    ("url", "extra_args"),
    [
        ("sbox", ["--sftp-pass", "UNIQUESECRET"]),
        ("sbox", ["--sftp-pass=UNIQUESECRET"]),
        ("sbox", ["--sftp-key-pem", "-----BEGIN KEY----- UNIQUESECRET -----END KEY-----"]),
        ("sbox", ["--sftp-key-pem=BEGIN KEY UNIQUESECRET DATA"]),
        ("web", ["--http-headers", "Authorization,Bearer UNIQUESECRET"]),
        ("web", ["--webdav-url", "https://dean:UNIQUESECRET@dav.example/remote.php"]),
        (":sftp,host=box,user=dean,pass=UNIQUESECRET", []),
    ],
    ids=["flag value", "equals form", "quoted with spaces", "equals form with spaces", "comma list", "url", "remote"],
)
def test_the_rclone_command_salmon_prints_hides_the_seedbox_secrets(
    monkeypatch, tmp_path, capfd, url: str, extra_args: list[str]
) -> None:
    _fake_rclone(monkeypatch, tmp_path)

    ok = _upload_with_fake_rclone(_box(url=url, extra_args=extra_args))
    assert ok is True

    out, err = capfd.readouterr()
    assert "Executing: rclone copy" in out
    assert "Rclone upload successful" in out
    assert "UNIQUESECRET" not in out + err


@needs_posix
def test_the_rclone_command_salmon_prints_keeps_harmless_values(monkeypatch, tmp_path, capfd) -> None:
    _fake_rclone(monkeypatch, tmp_path)
    extra_args = ["--checksum", "-P", "--sftp-path-override", "@/volume3", "--transfers", "4", "--bwlimit=8M"]

    _upload_with_fake_rclone(_box(url="sbox", extra_args=extra_args))

    assert (
        "Executing: rclone copy '/tmp/Artist - Album' 'sbox:/music/Artist - Album' "
        "--checksum -P --sftp-path-override @/volume3 --transfers 4 --bwlimit=8M"
    ) in capfd.readouterr().out


@needs_posix
def test_rclone_upload_folder_reports_nonzero_exit_code(monkeypatch, tmp_path, capfd) -> None:
    _fake_rclone(monkeypatch, tmp_path, exit_code=7)

    ok = _upload_with_fake_rclone(_box(url="seedbox", extra_args=["-P"]))
    out = capfd.readouterr().out

    assert ok is False
    assert "Rclone upload failed with exit code 7" in out


@needs_posix
@pytest.mark.parametrize(
    ("url", "extra_args", "echo"),
    [
        (
            ":sftp,host=box,user=dean,pass=UNIQUESECRET",
            [],
            'CRITICAL: Failed to create file system for ":sftp,host=box,user=dean,pass=UNIQUESECRET:/music": '
            "couldn't connect SSH",
        ),
        (
            ":webdav,url='https://dean:UNIQUESECRET@dav.example/'",
            [],
            "CRITICAL: Failed to create file system for \":webdav,url='https://dean:UNIQUESECRET@dav.example/':\": 401",
        ),
        (
            "sbox",
            ["-vv", "--sftp-pass", "UNIQUESECRET"],
            'DEBUG : rclone: Version "v1.75.1" starting with parameters ["rclone" "copy" "-vv" "--sftp-pass" '
            '"UNIQUESECRET"]',
        ),
        ("sbox", ["--sftp-pass", "UNIQUESECRET"], "ERROR : authentication failed for UNIQUESECRET"),
        (
            "web",
            ["--http-headers", "Authorization,Bearer UNIQUESECRET"],
            "ERROR : webdav answered 401 for Bearer UNIQUESECRET",
        ),
        # rclone --dump auth, with a token from rclone's own config that salmon never sees.
        ("web", ["--dump", "auth"], "DEBUG : HTTP REQUEST\nAuthorization: Bearer UNIQUESECRET\nUser-Agent: rclone"),
        ("sbox", ["--sftp-pass", "UNIQUESECRET"], "ERROR : no newline after UNIQUESECRET"),
    ],
    ids=["remote", "url in remote", "-vv command line", "flag value", "comma list", "dumped header", "last line"],
)
def test_what_rclone_echoes_on_stderr_is_masked(
    monkeypatch, tmp_path, capfd, url: str, extra_args: list[str], echo: str
) -> None:
    _fake_rclone(monkeypatch, tmp_path, stderr=echo if "no newline" in echo else echo + "\n", exit_code=1)

    ok = _upload_with_fake_rclone(_box(url=url, extra_args=extra_args))
    assert ok is False

    out, err = capfd.readouterr()
    # rclone's own line is still shown, only its secret is masked.
    assert echo[:20] in out
    assert "[REDACTED]" in out
    assert "UNIQUESECRET" not in out + err


class _RecordingClient:
    def __init__(self) -> None:
        self.save_paths: list[str] = []
        self.torrents: list[bytes] = []

    def add_to_downloader(self, remote_folder, torrent, is_paused, label) -> bool:
        self.save_paths.append(remote_folder)
        self.torrents.append(torrent)
        return True


def _run_upload(
    monkeypatch,
    tmp_path,
    seedboxes: list[Seedbox],
    rclone_exit_codes: dict[str, int] | None = None,
) -> tuple[dict[str, _RecordingClient], list[list[str]]]:
    """Upload one release to seedboxes with a fake client; rclone exits per URL, default 0."""
    clients: dict[str, _RecordingClient] = {}
    rclone_calls: list[list[str]] = []
    rclone_exit_codes = rclone_exit_codes or {}

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
        rclone_calls.append(commands)
        # commands[3] is "<url>:<remote_path>"; recover the seedbox url to look up its exit code.
        url = commands[3].split(":", 1)[0]
        return rclone_exit_codes.get(url, 0)

    monkeypatch.setattr(seedbox.cfg, "seedbox", seedboxes)
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    _route_rclone(monkeypatch, fake_run_rclone)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent = tmp_path / "Artist - Album.torrent"
    torrent.write_bytes(b"d4:infod4:name5:Albumee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent), task_type="seed", is_flac=True, folder=str(release))
    anyio.run(manager.execute_upload)
    return clients, rclone_calls


def test_disabled_seedbox_is_skipped(monkeypatch, tmp_path) -> None:
    # A disabled local entry listed before the rclone one used to add the torrent first, with the
    # local download_directory as its save path (#478).
    clients, rclone_calls = _run_upload(
        monkeypatch,
        tmp_path,
        [
            _box(type="local", enabled=False, torrent_client="qbittorrent+http://local:8080"),
            _box(type="rclone", enabled=False, url="old", torrent_client="qbittorrent+http://old:8080"),
            _box(
                type="rclone",
                enabled=True,
                url="box",
                directory="/home/user/files",
                torrent_client="qbittorrent+http://box:8080",
            ),
        ],
    )

    assert list(clients) == ["qbittorrent+http://box:8080"]
    assert clients["qbittorrent+http://box:8080"].save_paths == ["/home/user/files"]
    assert [call[3] for call in rclone_calls] == ["box:/home/user/files/Artist - Album (2020) [WEB FLAC]"]


def test_seedbox_without_enabled_key_is_skipped(monkeypatch, tmp_path) -> None:
    # The fork's default: a seedbox takes part only once it says enabled = true.
    entry = msgspec.toml.decode(
        b'type = "rclone"\nurl = "box"\ndirectory = "/files"\ntorrent_client = "qbittorrent+http://box:8080"\n',
        type=Seedbox,
    )

    clients, rclone_calls = _run_upload(monkeypatch, tmp_path, [entry])

    assert (clients, rclone_calls) == ({}, [])


def test_failed_rclone_copy_skips_seeding_on_that_seedbox(monkeypatch, tmp_path) -> None:
    # A failed rclone copy used to be reported but not acted on: the seed task still added
    # the torrent, so the client checked or downloaded a folder that was never uploaded (#503).
    clients, rclone_calls = _run_upload(
        monkeypatch,
        tmp_path,
        [
            _box(
                type="rclone",
                url="box",
                directory="/files",
                torrent_client="qbittorrent+http://box:8080",
            ),
        ],
        rclone_exit_codes={"box": 1},
    )

    assert len(rclone_calls) == 1
    assert clients["qbittorrent+http://box:8080"].save_paths == []


@needs_posix
def test_a_failed_copy_and_its_skipped_seed_are_reported_not_all_processed(monkeypatch, tmp_path, capfd) -> None:
    # A failed rclone copy used to still end with the green "All upload tasks processed" line,
    # because only add_to_downloader failures were counted towards the summary.
    _fake_rclone(monkeypatch, tmp_path, exit_code=1)
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="rclone", url="box", directory="/files", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RecordingClient()))

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capfd.readouterr().out
    assert "All upload tasks processed" not in out
    assert "1 copy and 1 seed task failed" in out


def test_failed_rclone_copy_does_not_affect_other_seedboxes(monkeypatch, tmp_path) -> None:
    clients, rclone_calls = _run_upload(
        monkeypatch,
        tmp_path,
        [
            _box(
                type="rclone",
                url="broken",
                directory="/files",
                torrent_client="qbittorrent+http://broken:8080",
            ),
            _box(
                type="rclone",
                url="good",
                directory="/files",
                torrent_client="qbittorrent+http://good:8080",
            ),
        ],
        rclone_exit_codes={"broken": 1},
    )

    assert len(rclone_calls) == 2
    assert clients["qbittorrent+http://broken:8080"].save_paths == []
    assert clients["qbittorrent+http://good:8080"].save_paths == ["/files"]


def test_successful_rclone_copy_still_seeds(monkeypatch, tmp_path) -> None:
    clients, rclone_calls = _run_upload(
        monkeypatch,
        tmp_path,
        [
            _box(
                type="rclone",
                url="box",
                directory="/files",
                torrent_client="qbittorrent+http://box:8080",
            ),
        ],
    )

    assert len(rclone_calls) == 1
    assert clients["qbittorrent+http://box:8080"].save_paths == ["/files"]


def test_failed_copy_only_skips_seeding_its_own_folder(monkeypatch, tmp_path) -> None:
    # A run queues several releases (a FLAC plus each transcode): a failed copy skips only its own
    # folder's seed, not every seed queued for that seedbox.
    clients: dict[str, _RecordingClient] = {}
    rclone_calls: list[list[str]] = []

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
        rclone_calls.append(commands)
        local_path = commands[2]
        returncode = 1 if "Album2" in local_path else 0
        return returncode

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="rclone", url="box", directory="/files", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    _route_rclone(monkeypatch, fake_run_rclone)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release1 = tmp_path / "Artist - Album1 (2020) [WEB FLAC]"
    release1.mkdir()
    release2 = tmp_path / "Artist - Album2 (2020) [WEB FLAC]"
    release2.mkdir()
    torrent1 = tmp_path / "Album1.torrent"
    torrent1.write_bytes(b"d4:infod4:name6:Album1ee")
    torrent2 = tmp_path / "Album2.torrent"
    torrent2.write_bytes(b"d4:infod4:name6:Album2ee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release1), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent1), task_type="seed", is_flac=True, folder=str(release1))
    manager.add_upload_task(str(release2), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent2), task_type="seed", is_flac=True, folder=str(release2))
    anyio.run(manager.execute_upload)

    assert len(rclone_calls) == 2
    client = clients["qbittorrent+http://box:8080"]
    assert client.torrents == [torrent1.read_bytes()]


def test_failed_copy_skips_both_torrents_of_multi_tracker_upload(monkeypatch, tmp_path) -> None:
    # multi_tracker_upload builds two torrents (RED and OPS) from the same release folder. A
    # failed copy of that folder must skip both seed tasks, not just the first one queued.
    clients: dict[str, _RecordingClient] = {}
    rclone_calls: list[list[str]] = []

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
        rclone_calls.append(commands)
        return 1

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="rclone", url="box", directory="/files", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    _route_rclone(monkeypatch, fake_run_rclone)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent_red = tmp_path / "Album [RED].torrent"
    torrent_red.write_bytes(b"d4:infod4:name5:Redeee")
    torrent_ops = tmp_path / "Album [OPS].torrent"
    torrent_ops.write_bytes(b"d4:infod4:name5:Opsxee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent_red), task_type="seed", is_flac=True, folder=str(release))
    manager.add_upload_task(str(torrent_ops), task_type="seed", is_flac=True, folder=str(release))
    anyio.run(manager.execute_upload)

    assert len(rclone_calls) == 1
    client = clients["qbittorrent+http://box:8080"]
    assert client.torrents == []


def test_rclone_not_installed_skips_seeding(monkeypatch, tmp_path) -> None:
    # A folder task that raises (rclone missing, unexpected I/O error, ...) must count as a
    # failed copy too, not just a logged and forgotten "critical error".
    clients: dict[str, _RecordingClient] = {}

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
        raise FileNotFoundError("rclone")

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="rclone", url="box", directory="/files", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    _route_rclone(monkeypatch, fake_run_rclone)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent = tmp_path / "Album.torrent"
    torrent.write_bytes(b"d4:infod4:name5:Albumee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent), task_type="seed", is_flac=True, folder=str(release))
    anyio.run(manager.execute_upload)

    client = clients["qbittorrent+http://box:8080"]
    assert client.torrents == []


def _queue_one_release(tmp_path) -> "seedbox.UploadManager":
    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent = tmp_path / "Album.torrent"
    torrent.write_bytes(b"d4:infod4:name5:Albumee")
    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True)
    manager.add_upload_task(str(torrent), task_type="seed", is_flac=True, folder=str(release))
    return manager


@needs_posix
def test_the_upload_run_never_prints_a_remotes_password(monkeypatch, tmp_path, capfd) -> None:
    # The remote is named when the uploader is configured and when a failed copy skips the seed.
    _fake_rclone(monkeypatch, tmp_path, exit_code=1)
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="rclone", url=":sftp,host=box,pass=UNIQUESECRET", torrent_client="qbittorrent+http://box:8080")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RecordingClient()))

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out, err = capfd.readouterr()
    assert "Configured rclone uploader to :sftp,host=box,pass=[REDACTED]" in out
    assert "Skipping seed on :sftp,host=box,pass=[REDACTED]" in out
    assert "UNIQUESECRET" not in out + err


def test_a_torrent_client_that_fails_to_configure_never_prints_its_password(monkeypatch, capsys) -> None:
    def refuse(url):
        raise ValueError(f"cannot parse {url}, password UNIQUESECRET")

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="local", torrent_client="qbittorrent+http://dean:UNIQUESECRET@box:8080")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(refuse))

    seedbox.UploadManager()

    out = capsys.readouterr().out
    assert "Failed to configure local uploader" in out
    assert "UNIQUESECRET" not in out


def test_a_failed_task_never_prints_the_seedbox_secrets(monkeypatch, tmp_path, capsys) -> None:
    async def refuse(commands: list[str], secrets: list[str]) -> int:
        raise OSError(f"could not start {' '.join(commands)}")

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="rclone", url="box", extra_args=["--sftp-pass", "UNIQUESECRET"], torrent_client="x")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RecordingClient()))
    _route_rclone(monkeypatch, refuse)

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "rclone could not start: could not start rclone copy" in out
    assert "UNIQUESECRET" not in out


def test_a_torrent_the_client_refuses_never_prints_its_password(monkeypatch, tmp_path, capsys) -> None:
    class Refusing:
        def torrents_add(self, **kwargs):
            raise RuntimeError("POST http://dean:UNIQUESECRET@box:8080/api/v2/torrents/add failed for UNIQUESECRET")

    monkeypatch.setattr(seedbox.cfg, "seedbox", [_box(type="local", torrent_client="unused")])
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: client))
    monkeypatch.setattr(QBittorrentClient, "login", lambda self: Refusing())
    client = QBittorrentClient(username="dean", password="UNIQUESECRET", url="http://box:8080")

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "Failed to add torrent" in out
    assert "UNIQUESECRET" not in out


class _RaisingClient:
    """A torrent client whose own add_to_downloader raises instead of catching its error."""

    def add_to_downloader(self, remote_folder, torrent, is_paused, label) -> bool:
        raise RuntimeError("connection reset for UNIQUESECRET")


def test_seedbox_names_itself_when_the_client_raises_unexpectedly(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="local", name="My Box", torrent_client="qbittorrent+http://dean:UNIQUESECRET@box:8080")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RaisingClient()))

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "Failed to add torrent to client on My Box" in out
    assert "UNIQUESECRET" not in out


def _connected_client(monkeypatch, cls, fake_client):
    """Build a torrent client whose login() returns fake_client without touching a network."""
    monkeypatch.setattr(cls, "login", lambda self: fake_client)
    return cls(username="dean", password="UNIQUESECRET", url="http://box:8080", host="box", port=1)


def test_qbittorrent_fails_response_is_reported_as_not_added(monkeypatch, capsys) -> None:
    # qbittorrent-api's torrents_add returns "Fails." rather than raising, most commonly for a
    # duplicate torrent already in the client.
    class FakeApi:
        def torrents_add(self, **kwargs):
            return "Fails."

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    out = capsys.readouterr().out
    assert "successfully" not in out.lower()


def test_qbittorrent_ok_response_is_reported_as_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def torrents_add(self, **kwargs):
            return "Ok."

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is True
    out = capsys.readouterr().out
    assert "Torrent added successfully" in out


def test_qbittorrent_5_1_metadata_success_is_reported_as_added(monkeypatch, capsys) -> None:
    # Web API v2.14.0+ (qBittorrent 5.1+) answers with a JSON object instead of "Ok."/"Fails.".
    class FakeApi:
        def torrents_add(self, **kwargs):
            return {"success_count": 1, "failure_count": 0, "pending_count": 0, "added_torrent_ids": ["abc"]}

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is True
    out = capsys.readouterr().out
    assert "Torrent added successfully" in out


def test_qbittorrent_5_1_metadata_failure_is_reported_as_not_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def torrents_add(self, **kwargs):
            return {"success_count": 0, "failure_count": 1, "pending_count": 0, "added_torrent_ids": []}

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    out = capsys.readouterr().out
    assert "successfully" not in out.lower()


def test_deluge_none_result_is_reported_as_not_added(monkeypatch, capsys) -> None:
    # core.add_torrent_file returns None when the torrent is refused (already present).
    class FakeApi:
        def call(self, *args, **kwargs):
            return None

    client = _connected_client(monkeypatch, DelugeClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    out = capsys.readouterr().out
    assert "successfully" not in out.lower()


def test_deluge_torrent_id_is_reported_as_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def call(self, *args, **kwargs):
            return "abc123"

    client = _connected_client(monkeypatch, DelugeClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is True
    out = capsys.readouterr().out
    assert "Torrent added successfully" in out


def test_transmission_torrent_result_is_reported_as_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def add_torrent(self, **kwargs):
            return object()

    client = _connected_client(monkeypatch, TransmissionClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is True
    out = capsys.readouterr().out
    assert "Torrent added successfully" in out


def test_a_client_call_that_raises_is_reported_as_not_added(monkeypatch, capsys) -> None:
    class FakeApi:
        def torrents_add(self, **kwargs):
            raise RuntimeError("connection reset")

    client = _connected_client(monkeypatch, QBittorrentClient, FakeApi())

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    out = capsys.readouterr().out
    assert "successfully" not in out.lower()


def test_a_client_that_never_connected_is_reported_as_not_added(monkeypatch, capsys) -> None:
    client = _connected_client(monkeypatch, QBittorrentClient, None)

    added = client.add_to_downloader("/music", b"torrent", is_paused=False, label="")

    assert added is False
    out = capsys.readouterr().out
    assert "successfully" not in out.lower()


class _RefusingClient:
    """A torrent client the seedbox layer sees as connected, but that never adds the torrent."""

    def add_to_downloader(self, remote_folder, torrent, is_paused, label) -> bool:
        return False


def test_seedbox_reports_a_refused_torrent_plainly_by_name(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="local", name="My Box", torrent_client="unused")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RefusingClient()))

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "Torrent added to client successfully" not in out
    assert "Torrent was not added to the client on My Box" in out
    assert "seed task" in out.lower()
    assert "failed" in out.lower()


def test_seedbox_reports_a_refused_torrent_by_masked_url_without_a_name(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [_box(type="rclone", url=":sftp,host=box,pass=UNIQUESECRET", torrent_client="unused", directory="/x")],
    )
    monkeypatch.setattr(seedbox.TorrentClientGenerator, "parse_libtc_url", staticmethod(lambda url: _RefusingClient()))
    monkeypatch.setattr(seedbox, "_rclone_upload_folder", lambda seedbox, remote, path: _async_true())

    anyio.run(_queue_one_release(tmp_path).execute_upload)

    out = capsys.readouterr().out
    assert "Torrent added to client successfully" not in out

    assert "Torrent was not added to the client on :sftp,host=box,pass=[REDACTED]" in out
    assert "UNIQUESECRET" not in out


def test_red_and_ops_upload_seeds_only_the_pinned_box_for_each_and_copies_the_folder_once(
    monkeypatch, tmp_path
) -> None:
    # One release uploaded to both RED and OPS, with a seedbox pinned to each: the folder must be
    # copied once per box that serves either tracker, and each box's seed must be its own torrent.
    clients: dict[str, _RecordingClient] = {}
    rclone_calls: list[list[str]] = []

    async def fake_run_rclone(commands: list[str], secrets: list[str]) -> int:
        rclone_calls.append(commands)
        return 0

    monkeypatch.setattr(
        seedbox.cfg,
        "seedbox",
        [
            _box(
                name="red-box",
                trackers=["RED"],
                type="rclone",
                url="red",
                directory="/files/red",
                torrent_client="qbittorrent+http://red:8080",
            ),
            _box(
                name="ops-box",
                trackers=["OPS"],
                type="rclone",
                url="ops",
                directory="/files/ops",
                torrent_client="qbittorrent+http://ops:8080",
            ),
        ],
    )
    monkeypatch.setattr(
        seedbox.TorrentClientGenerator,
        "parse_libtc_url",
        staticmethod(lambda url: clients.setdefault(url, _RecordingClient())),
    )
    _route_rclone(monkeypatch, fake_run_rclone)
    monkeypatch.setattr(seedbox.click, "secho", lambda *args, **kwargs: None)

    release = tmp_path / "Artist - Album (2020) [WEB FLAC]"
    release.mkdir()
    torrent_red = tmp_path / "Album [RED].torrent"
    torrent_red.write_bytes(b"d4:infod4:name5:Redeee")
    torrent_ops = tmp_path / "Album [OPS].torrent"
    torrent_ops.write_bytes(b"d4:infod4:name5:Opsxee")

    manager = seedbox.UploadManager()
    manager.add_upload_task(str(release), task_type="folder", is_flac=True, site_code="RED")
    manager.add_upload_task(str(torrent_red), task_type="seed", is_flac=True, folder=str(release), site_code="RED")
    manager.add_upload_task(str(release), task_type="folder", is_flac=True, site_code="OPS")
    manager.add_upload_task(str(torrent_ops), task_type="seed", is_flac=True, folder=str(release), site_code="OPS")
    anyio.run(manager.execute_upload)

    # One rclone copy per box, not one per (box, tracker) pair.
    assert len(rclone_calls) == 2
    assert clients["qbittorrent+http://red:8080"].torrents == [torrent_red.read_bytes()]
    assert clients["qbittorrent+http://ops:8080"].torrents == [torrent_ops.read_bytes()]
