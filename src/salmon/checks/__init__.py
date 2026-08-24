import os

import asyncclick as click

import salmon.trackers
from salmon.checks.integrity import handle_integrity_check
from salmon.checks.logs import check_log_cambia
from salmon.checks.mqa import check_mqa
from salmon.checks.upconverts import test_upconverted
from salmon.common import commandgroup
from salmon.constants import SOURCES
from salmon.errors import CRCMismatchError, EditedLogError


@commandgroup.group()
def check():
    """Check/evaluate various aspects of files and folders"""
    pass


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
async def log(path: str) -> None:
    """Check the score of log file(s).

    Args:
        path: Path to a log file or directory containing log files.
    """
    if os.path.isfile(path):
        await _check_log(path)
    elif os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                if f.lower().endswith(".log"):
                    filepath = os.path.join(root, f)
                    click.secho(f"\nScoring {filepath}...", fg="cyan")
                    await _check_log(filepath)


async def _check_log(path: str) -> None:
    """Score a single log file and display the result.

    Args:
        path: Path to the log file to check.
    """
    try:
        await check_log_cambia(path, os.path.dirname(path))
    except EditedLogError:
        click.secho("Error: Edited logs detected!", fg="red", bold=True)
    except CRCMismatchError:
        click.secho("Error: CRC mismatch between log and audio files!", fg="red", bold=True)
    except Exception as e:
        click.secho(f"Error checking log: {e}", fg="red")


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
async def upconv(path: str) -> None:
    """Check a 24bit FLAC file for upconversion.

    Args:
        path: Path to the FLAC file or directory to check.
    """
    await test_upconverted(path)


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
async def integrity(path: str) -> None:
    """Check the integrity of audio files.

    Args:
        path: Path to the audio file or directory to check.
    """
    await handle_integrity_check(path)


@check.command()
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
async def mqa(path):
    """Check if a FLAC file is MQA"""
    if os.path.isfile(path):
        if await check_mqa(path):
            click.secho("MQA syncword present", fg="red")
        else:
            click.secho("Did not find MQA syncword", fg="green")
    elif os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    filepath = os.path.join(root, f)
                    click.secho(f"\nChecking {filepath}...", fg="cyan")
                    if await check_mqa(filepath):
                        click.secho("MQA syncword present", fg="red")
                    else:
                        click.secho("Did not find MQA syncword", fg="green")


async def mqa_test(path: str) -> None:
    """Check whether any audio file in a release carries the MQA syncword.

    Every file, not just the first: MQA on a later track is as much of a trump as
    on track one, and this is the gate the upload itself runs — it must not be
    weaker than the advisory album check, which has always scanned them all.

    Args:
        path: Path to the FLAC file or directory to check.

    Raises:
        click.Abort: If MQA syncword is detected.
    """
    if os.path.isfile(path):
        if await check_mqa(path):
            click.secho(f"MQA syncword present in '{path}'", fg="red", bold=True)
            raise click.Abort
        return
    if not os.path.isdir(path):
        return

    from salmon.checks.album import run_mqa_check

    result = await run_mqa_check(path)
    hits = [f["file"] for f in result["files"] if f["detected"]]
    if hits:
        for name in hits:
            click.secho(f"MQA syncword present in '{name}'", fg="red", bold=True)
        raise click.Abort


@check.command(name="all")
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option(
    "--source", "-s", default=None, help=f"Media source ({'/'.join(SOURCES.values())}); inferred when omitted"
)
@click.option("--tracker", "-t", "trackers", multiple=True, help="Also search this tracker for duplicates")
async def all_checks(path: str, source: str | None, trackers: tuple[str, ...]) -> None:
    """Run every check over an album and print a single verdict.

    Exits non-zero when a release is unfit to upload.
    """
    from salmon.checks.preflight import BLOCK, OK, SKIP, WARN, run_checks

    for code in trackers:
        if code.upper() not in salmon.trackers.tracker_list:
            raise click.UsageError(f"Unknown tracker {code}. Configured: {', '.join(salmon.trackers.tracker_list)}")

    if source and source not in SOURCES.values():
        raise click.UsageError(f"Unknown source {source}. Valid sources: {', '.join(SOURCES.values())}")

    result = await run_checks(path, None, source, [c.upper() for c in trackers])
    colours = {OK: "green", WARN: "yellow", BLOCK: "red", SKIP: None}
    marks = {OK: "OK  ", WARN: "WARN", BLOCK: "FAIL", SKIP: "--  "}
    click.secho(f"\n{os.path.basename(path)}", bold=True)
    for row in result["rows"]:
        verdict = row["verdict"]
        click.secho(f"  {marks[verdict]}  ", fg=colours[verdict], bold=verdict == BLOCK, nl=False)
        click.secho(f"{row['label']:<18}", bold=True, nl=False)
        click.echo(row["detail"])

    # Not picking a source blocks the upload button, but this command is a
    # diagnostic — nothing is being claimed, so only real defects fail it.
    blocking = [b for b in result["blocking"] if source or b != "source"]
    if blocking:
        click.secho(f"\nNot suitable for upload: {len(blocking)} blocking issue(s).", fg="red", bold=True)
        raise SystemExit(1)
    if result["warnings"]:
        click.secho(f"\nUploadable, with {len(result['warnings'])} thing(s) to check first.", fg="yellow")
        return
    click.secho("\nAll checks passed.", fg="green", bold=True)
