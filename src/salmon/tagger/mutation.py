"""Shared policy for mutations that touch many files in one album.

None of these can be made atomic — writing tags to twelve files is twelve
writes — so when one fails partway the album is left inconsistent. Continuing
would upload a release whose tracks disagree with each other, so every such
failure reports how far it got and stops.
"""

import os
from typing import NoReturn

import asyncclick as click


def abort_partial(operation: str, done: list[str], remaining: list[str], error: BaseException) -> NoReturn:
    """Report how far a multi-file mutation got, then abort the run."""
    click.secho(f"\n{operation} failed: {error}", fg="red", bold=True)
    if done:
        click.secho(f"Already changed: {', '.join(done)}.", fg="yellow")
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
            os.rename(old, new)
            completed.append((old, new))
            click.echo(f" >> {new}")
    except BaseException as e:
        for old, new in reversed(completed):
            try:
                os.rename(new, old)
            except OSError:
                restore_failed = True
                click.secho(f"Could not restore {old}", fg="red", bold=True)
        # Saying "rolled back" after a failed restore would be the opposite of true.
        outcome = "the rollback was incomplete" if restore_failed else "was rolled back"
        click.secho(f"\nRename failed and {outcome}: {e}", fg="red", bold=True)
        raise click.Abort from e
