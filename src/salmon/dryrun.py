"""`salmon up --dry-run`: go through a whole upload on a scratch copy and send nothing."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Self

import asyncclick as click

from salmon.errors import DryRunRefused

# Context variables, so the flag and the scratch directory never outlive the run or the upload that set them.
_running: ContextVar[bool] = ContextVar("dry_run", default=False)
# Where an upload in a dry run writes what it would leave behind (torrent files, transcodes).
_scratch_dir: ContextVar[str | None] = ContextVar("dry_run_scratch_dir", default=None)


@contextmanager
def mode(on: bool = True) -> Iterator[None]:
    """Run the block as a dry run (if `on`), and every task it starts."""
    if not on:
        yield
        return
    token = _running.set(True)
    try:
        yield
    finally:
        _running.reset(token)


def active() -> bool:
    """Whether a dry run is running."""
    return _running.get()


def refuse(action: str) -> None:
    """Stop a step that would send something (`action`, e.g. "send POST ... to RED"): DryRunRefused in a dry run."""
    # The steps that send skip themselves in a dry run; this stops any that was missed, before anything goes out.
    if active():
        raise DryRunRefused(
            f"Dry run stopped before it could {action}. Nothing was sent. A step that sends something ran "
            "during a dry run: this is a bug in salmon, please report it."
        )


def say(message: str) -> None:
    """Print what a dry run does instead of a step: "Dry run: " and the message."""
    click.secho(f"Dry run: {message}", fg="cyan")


@contextmanager
def writing_into(path: str | None) -> Iterator[None]:
    """Make path (the upload's scratch run directory, or None) where this upload writes, for the block only."""
    token = _scratch_dir.set(path)
    try:
        yield
    finally:
        _scratch_dir.reset(token)


def scratch_dir() -> str:
    """Where an upload in a dry run writes torrent files and transcodes; RuntimeError outside one."""
    # The run directory of the album's scratch copy, removed when the upload ends: no torrent file is left
    # where a torrent client could pick it up.
    path = _scratch_dir.get()
    if not active() or path is None:
        raise RuntimeError("no dry run upload with a scratch copy is running")
    return path


class Pending(int):
    """An ID only the tracker gives, standing in for it (true, never a real ID) in a dry run's forms and links."""

    description: str

    def __new__(cls, description: str) -> Self:
        pending = super().__new__(cls, -1)
        pending.description = description
        return pending

    def __str__(self) -> str:
        return f"<{self.description}>"

    __repr__ = __str__


NEW_GROUP_ID = Pending("ID of the group this upload creates")
NEW_TORRENT_ID = Pending("ID of the uploaded torrent")


def image_url(path: str, host: str) -> str:
    """Stand in for the URL an image host would give the image at path, which a dry run does not upload."""
    return f"<{host} URL of {os.path.basename(path)}>"
