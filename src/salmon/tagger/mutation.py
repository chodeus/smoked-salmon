"""Shared policy for mutations that touch many files in one album.

None of these can be made atomic — writing tags to twelve files is twelve
writes — so when one fails partway the album is left inconsistent. Continuing
would upload a release whose tracks disagree with each other, so every such
failure reports how far it got and stops.
"""

import errno
import os
from typing import NoReturn

import asyncclick as click


def _rename_no_clobber(src: str, dst: str) -> None:
    """Rename without ever replacing an existing destination.

    os.rename silently overwrites on POSIX, so checking os.path.exists first
    leaves a window in which another process can create the target — and the
    overwritten file is not something a rollback can put back. Linking fails
    outright when the destination exists, which closes that window.
    """
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno in {errno.EEXIST}:
            raise
        # Filesystems without hardlinks (or cross-device): fall back, accepting
        # the check-then-rename window rather than failing the whole operation.
        if os.path.exists(dst):
            raise FileExistsError(errno.EEXIST, "Destination exists", dst) from e
        os.rename(src, dst)
        return
    os.unlink(src)


def abort_partial(
    operation: str,
    done: list[str],
    failed: str | None,
    remaining: list[str],
    error: BaseException,
) -> NoReturn:
    """Report how far a multi-file mutation got, then abort the run.

    `failed` is the file being written when it broke. A write can fail after
    changing the file, so its state is unknown rather than untouched — reporting
    it as unchanged would send someone looking in the wrong place.
    """
    click.secho(f"\n{operation} failed: {error}", fg="red", bold=True)
    if done:
        click.secho(f"Already changed: {', '.join(done)}.", fg="yellow")
    if failed:
        click.secho(f"May be partially changed: {failed}.", fg="red", bold=True)
    if done or failed:
        click.secho("The album is now inconsistent — repair it before uploading.", fg="red", bold=True)
    if remaining:
        click.secho(f"Not changed: {', '.join(remaining)}.", fg="yellow")
    raise click.Abort from error


def rename_all_or_none(renames: list[tuple[str, str]]) -> None:
    """Rename every pair, undoing the lot if any one fails.

    Unlike a tag write, a rename is reversible, so a partial run can be put back
    rather than merely reported.
    """
    completed: list[tuple[str, str]] = []
    restore_failed = False
    try:
        for old, new in renames:
            _rename_no_clobber(old, new)
            completed.append((old, new))
            click.echo(f" >> {new}")
    except BaseException as e:
        for old, new in reversed(completed):
            try:
                _rename_no_clobber(new, old)
            except OSError:
                restore_failed = True
                click.secho(f"Could not restore {old}", fg="red", bold=True)
        # Saying "rolled back" after a failed restore would be the opposite of true.
        outcome = "the rollback was incomplete" if restore_failed else "was rolled back"
        click.secho(f"\nRename failed and {outcome}: {e}", fg="red", bold=True)
        raise click.Abort from e
