import argparse
import collections
import os
import posixpath
from urllib.parse import unquote, urlparse

import anyio
import asyncclick as click

from salmon import cfg, dryrun
from salmon.common.redaction import redact_command, redact_secrets, secret_values
from salmon.config.validations import Seedbox
from salmon.errors import DryRunRefused
from salmon.uploader.torrent_client import TorrentClient, TorrentClientGenerator


def _resolve_shell_path(remote_folder: str, extra_args: list[str]) -> str:
    """Resolve the effective download path, respecting --sftp-path-override.

    Args:
        remote_folder: The base remote directory path.
        extra_args: Extra CLI arguments that may contain --sftp-path-override.

    Returns:
        Effective shell path for the torrent client.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--sftp-path-override", type=str, default=None)
    known_args, _ = parser.parse_known_args(extra_args)
    override = known_args.sftp_path_override
    if not override:
        return remote_folder
    if override.startswith("@"):
        return posixpath.join(override.removeprefix("@"), remote_folder.removeprefix("/"))
    return override


def seedbox_secrets(seedbox: Seedbox) -> list[str]:
    """The secrets a seedbox's rclone remote, extra_args and torrent client URL carry, to mask wherever echoed."""
    secrets = secret_values(seedbox.extra_args, seedbox.url)
    try:
        password = urlparse(seedbox.torrent_client).password
    except ValueError:
        password = None
    if password:
        # Written percent-encoded in the URL, and decoded by the time a client error repeats it.
        secrets += [password, unquote(password)]
    return secrets


async def _rclone_upload_folder(seedbox: Seedbox, remote_folder: str, path: str) -> bool:
    """Upload a local folder to the rclone remote and return whether rclone succeeded."""
    remote_path = posixpath.join(remote_folder, os.path.basename(path))
    commands = ["rclone", "copy", path, f"{seedbox.url}:{remote_path}", *seedbox.extra_args]
    secrets = seedbox_secrets(seedbox)
    click.secho(redact_secrets(f"Starting Rclone upload to {seedbox.url}:{remote_folder}", secrets), fg="cyan")
    click.secho(f"Executing: {redact_command(commands, secrets)}", fg="yellow")
    # Captured rather than passed to the terminal: the job log is where a failure has to be readable.
    try:
        result = await anyio.run_process(commands, check=False)
    except OSError as error:
        click.secho(f"rclone could not start: {redact_secrets(str(error), secrets)}", fg="red")
        return False
    if result.returncode == 0:
        click.secho(
            redact_secrets(f"Rclone upload successful: {path} to {seedbox.url}:{remote_path}", secrets), fg="green"
        )
        return True
    click.secho(f"Rclone upload failed with exit code {result.returncode}", fg="red")
    for line in _output_tail(result.stderr) or _output_tail(result.stdout):
        click.secho(f"  rclone: {redact_secrets(line, secrets)}", fg="red")
    return False


def _output_tail(output: bytes | None, lines: int = 12) -> list[str]:
    """Last non-empty lines of a captured stream, so rclone's own error reaches the log."""
    text = (output or b"").decode(errors="replace")
    return [line for line in text.splitlines() if line.strip()][-lines:]


async def _add_to_downloader(
    client: TorrentClient,
    shell_path: str,
    torrent_path: str,
    label: str,
    add_paused: bool,
    secrets: list[str],
    seedbox_name: str,
) -> bool:
    """Read a torrent file and add it to the download client.

    Args:
        client: Torrent client instance.
        shell_path: Download directory path passed to the client.
        torrent_path: Local path to the .torrent file.
        label: Label to apply in the download client.
        add_paused: Whether to add the torrent in paused state.
        secrets: Values to mask in an error, from seedbox_secrets.
        seedbox_name: The seedbox's name (or its masked url) to name in a failure line.

    Returns:
        True if the client reported the torrent was added, False otherwise.
    """
    async with await anyio.open_file(torrent_path, "rb") as f:
        torrent = await f.read()
    try:
        added = client.add_to_downloader(shell_path, torrent, is_paused=add_paused, label=label)
    except Exception as e:
        click.secho(f"Failed to add torrent to client on {seedbox_name}: {redact_secrets(str(e), secrets)}", fg="red")
        return False
    if not added:
        click.secho(f"Torrent was not added to the client on {seedbox_name}", fg="red")
        return False
    click.secho("Torrent added to client successfully", fg="green")
    return True


def _seedbox_display_name(seedbox: Seedbox, secrets: list[str]) -> str:
    """The name to blame in a failure line: the seedbox's `name`, else its masked `url`."""
    if seedbox.name:
        return seedbox.name
    return redact_secrets(seedbox.url, secrets)


def _enabled_seedboxes() -> list[Seedbox]:
    """Return the configured seedboxes that are not disabled with `enabled = false`."""
    return [seedbox for seedbox in cfg.seedbox if seedbox.enabled]


class UploadManager:
    """Collects upload and seed tasks during a session and executes them all at once.

    Folder tasks are prepended to the queue (run first) so files are present
    on the remote before the corresponding torrents are added to the client.
    """

    def __init__(self) -> None:
        self._client_cache: dict[str, TorrentClient] = {}
        # (seedbox, local_path, task_type, folder): folder is the release folder, so a failed copy
        # skips only that folder's seeds on that seedbox.
        self.tasks: collections.deque[tuple[Seedbox, str, str, str]] = collections.deque()
        if dryrun.active():
            # Setting up a torrent client logs into it. A dry run queues nothing, so it needs none.
            if _enabled_seedboxes():
                dryrun.say("not connecting to the seedboxes' torrent clients.")
            return

        click.secho("Initializing upload managers", fg="cyan")
        for seedbox in _enabled_seedboxes():
            secrets = seedbox_secrets(seedbox)
            try:
                if seedbox.torrent_client not in self._client_cache:
                    self._client_cache[seedbox.torrent_client] = TorrentClientGenerator.parse_libtc_url(
                        seedbox.torrent_client
                    )
                click.secho(
                    redact_secrets(f"Configured {seedbox.type} uploader to {seedbox.url}", secrets), fg="yellow"
                )
            except DryRunRefused:
                # Were the skip above missed, the client's refusal to log in must stop the run, not read as
                # a seedbox that failed to configure.
                raise
            except Exception as e:
                click.secho(f"Failed to configure {seedbox.type} uploader: {redact_secrets(str(e), secrets)}", fg="red")

    def _client(self, seedbox: Seedbox) -> TorrentClient:
        """Look up the cached torrent client for a seedbox entry.

        Args:
            seedbox: Seedbox config whose torrent_client URL is used as the cache key.

        Returns:
            The cached TorrentClient instance.
        """
        return self._client_cache[seedbox.torrent_client]

    def add_upload_task(
        self,
        directory: str,
        task_type: str,
        is_flac: bool,
        folder: str | None = None,
        site_code: str | None = None,
    ) -> None:
        """Queue upload tasks for a path across all configured seedboxes.

        Args:
            directory: Local folder path (for "folder" tasks) or .torrent file path (for "seed" tasks).
            task_type: Either "folder" to transfer files or "seed" to add to the download client.
            is_flac: Whether the release is FLAC; skips seedboxes with flac_only=True if False.
            folder: For a "seed" task, the release folder its torrent was built from, so a failed
                copy of that folder skips this seed. Ignored for a "folder" task, which always uses
                its own path. Defaults to `directory` when omitted, matching the old behaviour.
            site_code: Tracker this upload went to. Skips a seedbox whose `trackers` is set and
                does not contain it; a seedbox with no `trackers` still matches every tracker.

        Raises:
            DryRunRefused: In a dry run, which copies nothing to a seedbox and adds nothing to a client.
        """
        dryrun.refuse(f"queue {directory} for a seedbox copy or a torrent client")
        click.secho(f"Preparing upload tasks for: {directory}", fg="cyan")
        task_folder = directory if task_type == "folder" else (folder or directory)
        for seedbox in _enabled_seedboxes():
            if seedbox.torrent_client not in self._client_cache:
                continue
            if seedbox.flac_only and not is_flac:
                continue
            if seedbox.trackers and (site_code or "").upper() not in seedbox.trackers:
                continue
            task = (seedbox, directory, task_type, task_folder)
            if task in self.tasks:
                continue
            if task_type == "seed":
                self.tasks.append(task)
                click.secho("Added seed task", fg="magenta")
            elif task_type == "folder":
                self.tasks.appendleft(task)
                click.secho("Added folder transfer task", fg="magenta")

    async def execute_upload(self) -> None:
        """Execute all queued upload tasks in order (folders first, then seeds)."""
        if not self.tasks:
            click.secho("No upload tasks to execute", fg="yellow")
            return

        click.secho(f"Executing {len(self.tasks)} upload tasks", fg="cyan")
        # Keyed by (id(seedbox), folder): a failed copy of one folder must not skip the seed of a
        # different folder queued for the same seedbox.
        failed_folders: set[tuple[int, str]] = set()
        failed_copies = 0
        failed_seeds = 0
        for i, (seedbox, local_path, task_type, folder) in enumerate(self.tasks, 1):
            click.secho(
                f"\nTask {i}/{len(self.tasks)}: {task_type.upper()} - {os.path.basename(local_path)}",
                fg="cyan",
            )
            secrets = seedbox_secrets(seedbox)
            try:
                if task_type == "folder":
                    if seedbox.type == "rclone":
                        succeeded = await _rclone_upload_folder(seedbox, seedbox.directory, local_path)
                        if not succeeded:
                            failed_folders.add((id(seedbox), folder))
                            failed_copies += 1
                elif task_type == "seed":
                    if (id(seedbox), folder) in failed_folders:
                        click.secho(
                            redact_secrets(
                                f"Skipping seed on {seedbox.url}: the Rclone upload of {folder} failed, "
                                f"so {local_path} was not added to the client there. Add it by hand once "
                                "the files have been copied.",
                                secrets,
                            ),
                            fg="red",
                        )
                        failed_seeds += 1
                        continue
                    client = self._client(seedbox)
                    if seedbox.type == "rclone":
                        shell_path = _resolve_shell_path(seedbox.directory, seedbox.extra_args)
                    else:
                        shell_path = seedbox.directory or os.path.abspath(cfg.directory.download_directory)
                    added = await _add_to_downloader(
                        client,
                        shell_path,
                        local_path,
                        seedbox.label,
                        seedbox.add_paused,
                        secrets,
                        _seedbox_display_name(seedbox, secrets),
                    )
                    if not added:
                        failed_seeds += 1
            except Exception as e:
                click.secho(f"Critical error during task: {redact_secrets(str(e), secrets)}", fg="red")
                if task_type == "folder":
                    # A raising folder task is a failed copy too: its seeds must not be added.
                    failed_folders.add((id(seedbox), folder))
                    failed_copies += 1
                elif task_type == "seed":
                    failed_seeds += 1

        if failed_copies or failed_seeds:
            parts = []
            if failed_copies:
                parts.append(f"{failed_copies} {'copy' if failed_copies == 1 else 'copies'}")
            if failed_seeds:
                parts.append(f"{failed_seeds} seed task{'' if failed_seeds == 1 else 's'}")
            click.secho(f"\n{' and '.join(parts)} failed; see above", fg="red")
        else:
            click.secho("\nAll upload tasks processed", fg="green")
        self.tasks.clear()
