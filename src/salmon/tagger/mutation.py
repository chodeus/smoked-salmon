"""Shared policy for mutations that touch many files in one album.

None of these can be made atomic — writing tags to twelve files is twelve
writes — so when one fails partway the album is left inconsistent. Continuing
would upload a release whose tracks disagree with each other, so every such
failure reports how far it got and stops.
"""

from typing import NoReturn

import asyncclick as click


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
