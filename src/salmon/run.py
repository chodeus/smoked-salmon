import contextlib
import os
import shutil
import time

import asyncclick as click

import salmon.commands
import salmon.converter
import salmon.cross_upload
import salmon.images
import salmon.search
import salmon.tagger
import salmon.uploader
import salmon.webui
from salmon import cfg
from salmon.common import commandgroup
from salmon.errors import FilterError, LoginError, UploadError
from salmon.release_notification import show_release_notification

# Every salmon command runs this at startup, including one launched while
# `salmon web` is already serving, so anything recent may belong to that other
# process — its spectrals, its staging folder — and must be left alone.
TMP_MAX_AGE_HOURS = 24


def _newest_mtime(path: str) -> float:
    """Latest mtime of an entry, or of its direct children if it is a folder.

    A folder being written into right now keeps an old mtime of its own, so the
    children are what say whether it is still in use.
    """
    newest = os.path.getmtime(path)
    if os.path.isdir(path) and not os.path.islink(path):
        with os.scandir(path) as entries:
            for entry in entries:
                with contextlib.suppress(OSError):
                    newest = max(newest, entry.stat().st_mtime)
    return newest


def cleanup_tmp_dir():
    """Remove stale scratch from the temporary directory if configured.

    Age-gated rather than a full wipe: this is not the only salmon that may be
    using tmp_dir, and deleting a running webui's spectrals mid-session (or a
    folder mid-write) costs more than leaving a day of scratch behind.
    """
    if not (cfg.directory.tmp_dir and cfg.directory.clean_tmp_dir):
        return
    cutoff = time.time() - TMP_MAX_AGE_HOURS * 3600
    removed = kept = 0
    try:
        for item in os.listdir(cfg.directory.tmp_dir):
            item_path = os.path.join(cfg.directory.tmp_dir, item)
            try:
                if _newest_mtime(item_path) > cutoff:
                    kept += 1
                    continue
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                else:
                    shutil.rmtree(item_path)
                removed += 1
            except OSError as e:
                click.secho(f"Failed to remove {item_path}: {e}", fg="yellow")
        if removed or kept:
            click.secho(
                f"Cleaned {removed} stale item(s) from {cfg.directory.tmp_dir}"
                + (f"; left {kept} newer than {TMP_MAX_AGE_HOURS}h alone" if kept else ""),
                fg="green",
            )
    except OSError as e:
        click.secho(f"Failed to clean temporary directory: {e}", fg="yellow")


def main():
    try:
        cleanup_tmp_dir()
        show_release_notification()
        click.echo()

        commandgroup(obj={})
    except (UploadError, FilterError) as e:
        click.secho(f"There was an error: {e}", fg="red", bold=True)
    except ImportError as e:
        click.secho(f"You are missing required dependencies: {e}", fg="red")


if __name__ == "__main__":
    main()
