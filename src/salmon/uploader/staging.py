"""Copies of an album to work on, so the folder an upload starts from is never modified."""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

import asyncclick as click

from salmon import cfg
from salmon.converter.conversions import carry_conversion
from salmon.errors import UploadError

# Scratch copies live here, one directory per run, removed when it ends.
STAGING_DIR = ".salmon-staging"


@contextmanager
def staged_source(path: str, scratch: bool) -> Iterator[tuple[str, str | None]]:
    """The folder to work on, and the directory its rename stays in (None: download_directory).

    A scratch copy (--skip-flac-upload: never uploaded or seeded) is removed on exit; a library
    album is copied into download_directory and kept, as it is the upload; anything else is used as is.
    """
    if not scratch:
        yield (_stage_source(path) if cfg.directory.is_library_path(path) else path), None
        return
    scratch_dir = _new_scratch_dir()
    try:
        yield _stage_source(path, scratch_dir), scratch_dir
    finally:
        _remove_scratch_dir(scratch_dir, path)


def _stage_source(path: str, into: str | None = None) -> str:
    """Copy an album that must stay untouched into `into` (download_directory) before anything can mutate it.

    Must be a real copy, not a hardlink: a hardlink shares the inode, so the tag
    writes later in the flow would reach the source file too.
    """
    dest = os.path.join(into or cfg.directory.download_directory, os.path.basename(path.rstrip(os.sep)))
    if os.path.exists(dest):
        raise UploadError(f"Cannot stage the source, {dest} already exists.")
    click.secho(f"\nCopying {path} to {dest} (the source is never modified)...", fg="cyan")
    shutil.copytree(path, dest)
    # The record lives beside the album, not in it, so the copy would otherwise leave it behind.
    carry_conversion(path, dest)
    return dest


def _new_scratch_dir() -> str:
    """A directory of this run's own under download_directory/.salmon-staging; leftovers of other runs stay put."""
    root = os.path.join(cfg.directory.download_directory, STAGING_DIR)
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(dir=root, prefix="run-")


def _remove_scratch_dir(scratch_dir: str, source: str) -> None:
    """Remove a directory made by _new_scratch_dir, and nothing that resolves anywhere else."""
    root = os.path.realpath(os.path.join(cfg.directory.download_directory, STAGING_DIR))
    real = os.path.realpath(scratch_dir)
    real_source = os.path.realpath(source)
    # Checked on the resolved path: a symlinked component must not carry the rmtree out of the root.
    inside = os.path.dirname(real) == root and os.path.commonpath([real, real_source]) != real
    if os.path.islink(scratch_dir) or not inside or real == real_source:
        click.secho(f"Left the staged copy at {scratch_dir}: it is not inside {root}.", fg="yellow")
        return
    try:
        shutil.rmtree(real)
    except OSError as error:
        click.secho(f"Could not remove the staged copy at {scratch_dir}: {error}", fg="yellow")
