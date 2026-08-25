import os
import re
from contextlib import suppress

import anyio
import asyncclick as click
import msgspec

from salmon import cfg
from salmon.common.files import process_files

FLAC_IMPORTANT_REGEXES = [
    re.compile(r"(.+\.flac: testing,.*)\x08ok"),
    re.compile(r"(.+\.flac:.+)\nok\s*", re.MULTILINE),
]

FLAC_MD5_UNSET_RE = re.compile(r"WARNING.*MD5 signature.*STREAMINFO", re.IGNORECASE)

# What an unset MD5 actually means, said once per album rather than once per file.
# Worth spelling out because the files are fine: flac verifies the frame CRCs and
# prints "ok" — there is simply no whole-file checksum stored to check against.
MD5_UNSET_NOTE = (
    "The audio decodes cleanly; there is just no checksum stored to verify it against. "
    "Normal for WEB / web-store downloads.\n"
    "Sanitize re-encodes the album losslessly to set the MD5 (this also strips embedded art); "
    'note it in the release description: "WEB download, re-encoded to set MD5".'
)

# flac prints "ok" only once the stream has actually decoded, so its presence is a
# positive signal rather than the absence of an error. Verified shapes: a clean file
# gives "name.flac: ok", an unset MD5 gives the warning then a bare "ok", and a
# truncated file gives "ERROR while decoding metadata" and no "ok" at all.
FLAC_OK_RE = re.compile(r"(?m)^(?:.*: )?ok\s*$")

# mp3val prefixes every line it cares about. Each prefix has exactly one home:
# INFO and ERROR describe the file and belong in details, WARNING is a concern
# the UI chips separately — putting a line in both shows it to the reader twice.
MP3_INFO_PREFIX = "INFO:"
MP3_ERROR_PREFIX = "ERROR:"
MP3_WARNING_PREFIX = "WARNING:"


class IntegrityResult(msgspec.Struct, frozen=True):
    """Whether the files decoded, and anything the tools said short of failing.

    The two are separate on purpose: a file that will not decode is a different
    state from one that decodes with a complaint, and reporting the second as
    "decodes cleanly" is how an unearned green tick happens.
    """

    passed: bool
    details: str = ""
    concerns: tuple[str, ...] = ()
    # Names only. The explanation is MD5_UNSET_NOTE, rendered once for the whole set —
    # repeating it per file is what turned one fact about 17 tracks into 34 lines.
    md5_unset: tuple[str, ...] = ()
    # Files that failed for any reason other than a bare missing MD5. Kept separate so
    # the two can never be conflated: "3 of 17" is a part-WEB album, a decode failure is
    # a broken file, and only the first is safe to wave through.
    decode_failures: tuple[str, ...] = ()
    checked: int = 0


def _resolve_overstrikes(text: str) -> str:
    """Apply flac's progress backspaces instead of carrying them into the output.

    flac erases "testing, N% complete" with a run of \\x08 before writing the next
    message. Passed through raw, those bytes eat the characters in front of them
    wherever the details are rendered — "Eye of the Storm" + a warning became
    "Eye of the Stormgnature unset in STREAMINFO".
    """
    out: list[str] = []
    for char in text:
        if char == "\x08":
            if out and out[-1] != "\n":
                out.pop()
        elif char == "\r":
            while out and out[-1] != "\n":
                out.pop()
        else:
            out.append(char)
    return "".join(out)


def md5_unset_summary(count: int, checked: int) -> str:
    """ "3 of 17" reads differently from "17 of 17" — one is a part-WEB album."""
    return (
        f"{count} of {checked} file(s) have no stored MD5 signature"
        if checked
        else (f"{count} file(s) have no stored MD5 signature")
    )


def sanitize_prompt(result: IntegrityResult) -> str:
    """The question asked at the point of decision, naming the reason it is being asked.

    Lives here so the prompt and the explanation above it cannot drift apart.
    """
    if result.md5_unset and not result.decode_failures:
        return (
            f"{md5_unset_summary(len(result.md5_unset), result.checked)}. "
            "Re-encode the album losslessly to set it? (also strips embedded art)"
        )
    return "Do you want to sanitize this upload?"


def _describe_md5_unset(names: tuple[str, ...], checked: int) -> str:
    """The unset-MD5 section, once for the whole set.

    Names only when there are few of them — a whole album listed track by track is
    the wall of text this replaced, and the count already says everything.
    """
    head = md5_unset_summary(len(names), checked)
    if len(names) <= 3:
        head += f": {', '.join(names)}"
    return f"{head}.\n{MD5_UNSET_NOTE}"


def format_integrity(result: IntegrityResult) -> str:
    """Format the integrity check result for display."""
    integrities, integrities_out = result.passed, result.details
    if integrities:
        if result.concerns:
            return click.style(
                f"Passed integrity check, with {len(result.concerns)} warning(s):\n" + "\n".join(result.concerns),
                fg="yellow",
            )
        return click.style("Passed integrity check", fg="green")
    else:
        # Name the reason in the headline. A missing checksum and a file that will not
        # decode both stop the upload, but only one of them means the audio is suspect.
        # Gated on decode_failures, not on details: a failure that parsed to no detail
        # text would otherwise be announced as a clean "no MD5 signature".
        if result.md5_unset and not result.decode_failures:
            output = click.style("Integrity check not passed — no MD5 signature stored", fg="red", bold=True)
        else:
            output = click.style("Failed integrity check", fg="red", bold=True)
        if result.md5_unset:
            output += "\n\n" + click.style(_describe_md5_unset(result.md5_unset, result.checked), fg="yellow")
        if integrities_out:
            output += f"\nDetails:\n{integrities_out}"
        return output


async def handle_integrity_check(path: str) -> None:
    """Handle the integrity check process including UI and sanitization.

    Args:
        path: Path to a file or directory to check.

    Raises:
        click.Abort: If the path is neither a file nor a directory.
    """
    if os.path.isfile(path):
        if not any(path.lower().endswith(ext) for ext in [".flac", ".mp3"]):
            click.secho(f"File '{path}' is not a FLAC or MP3 file.", fg="red", bold=True)
            return

        result = await check_integrity(path)
        click.echo(format_integrity(result))

        if (
            not result.passed
            and path.lower().endswith(".flac")
            and click.confirm(click.style(f"\n{sanitize_prompt(result)}", fg="magenta"), default=True)
        ):
            await sanitize_and_verify(path)
    elif os.path.isdir(path):
        result = await check_integrity(path)
        click.echo(format_integrity(result))

        if not result.passed and click.confirm(click.style(f"\n{sanitize_prompt(result)}", fg="magenta"), default=True):
            await sanitize_and_verify(path)
    else:
        raise click.Abort


async def resolve_integrity_for_upload(path: str, *, scene: bool, assume_yes: bool) -> IntegrityResult:
    """Check the folder, offer to sanitize, and abort if anything still will not decode.

    Owns the whole "is this folder fit to upload?" decision so the CLI has one gate
    rather than a condition the caller can accidentally re-order.
    """
    click.secho("\nChecking integrity of audio files...", fg="cyan", bold=True)
    result = await check_integrity(path)
    click.echo(format_integrity(result))
    if result.passed:
        return result

    if scene:
        click.secho(
            "Some files failed sanitization, and this a scene release. "
            "You need to sanitize and de-scene before uploading. Aborting.",
            fg="red",
            bold=True,
        )
        raise click.Abort()

    if assume_yes or click.confirm(click.style(f"\n{sanitize_prompt(result)}", fg="magenta"), default=True):
        result = await sanitize_and_verify(path)

    # Outside the sanitize branch: declining to sanitize is not consent to upload files
    # that will not decode. Ungated by assume_yes too — that means "take the default
    # answer", not "upload anything".
    if result.decode_failures:
        click.secho(f"{len(result.decode_failures)} file(s) do not decode. Aborting.", fg="red", bold=True)
        raise click.Abort()
    return result


async def sanitize_and_verify(path: str) -> IntegrityResult:
    """Sanitize, then re-check, and report what the re-check found.

    The re-check is the point. Callers are about to act on "the MD5 is set now" —
    the upload even says so in the release description — so the claim has to be
    verified rather than inferred from sanitize_integrity's return value.
    """
    click.secho("\nSanitizing...", fg="cyan", bold=True)
    reported_ok = await sanitize_integrity(path)
    result = await check_integrity(path)
    click.echo(format_integrity(result))
    if not result.passed:
        click.secho("Sanitization did not clear the integrity check.", fg="red", bold=True)
    elif not reported_ok:
        click.secho("A file reported a sanitization error, but the check now passes.", fg="yellow")
    else:
        click.secho("Sanitization complete", fg="green")
    return result


async def check_integrity(path: str, _: int | None = None) -> IntegrityResult:
    """Check the integrity of audio files at the given path.

    Args:
        path: Path to a FLAC/MP3 file or a directory containing audio files.
        _: Unused index parameter for process_files compatibility.

    Returns:
        An IntegrityResult: whether everything decoded, plus anything worth reading.

    Raises:
        click.Abort: If no audio files found or path is invalid.
    """
    if path.lower().endswith(".flac"):
        return await _check_flac_integrity(path)
    elif path.lower().endswith(".mp3"):
        return await _check_mp3_integrity(path)
    elif os.path.isdir(path):
        integrities_out: list[str] = []
        integrities = True
        audio_files: list[str] = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    audio_files.append(os.path.join(root, f))
        if not audio_files:
            click.secho("No audio files found in directory", fg="red", bold=True)
            raise click.Abort
        results = await process_files(audio_files, check_integrity, "Checking audio files")
        concerns: list[str] = []
        md5_unset: list[str] = []
        decode_failures: list[str] = []
        checked = 0
        for result in results:
            integrities = integrities and result.passed
            # Skip empties, or a clean album joins into a run of blank lines.
            if result.details:
                integrities_out.append(result.details)
            concerns.extend(result.concerns)
            md5_unset.extend(result.md5_unset)
            decode_failures.extend(result.decode_failures)
            checked += result.checked
        return IntegrityResult(
            integrities,
            "\n".join(integrities_out),
            tuple(concerns),
            tuple(md5_unset),
            tuple(decode_failures),
            checked,
        )
    raise click.Abort


async def _check_flac_integrity(path: str) -> IntegrityResult:
    """Check the integrity of a single FLAC file using the flac CLI.

    Args:
        path: Path to the FLAC file.

    Returns:
        An IntegrityResult built from the tool's output.
    """
    try:
        result = await anyio.run_process(["flac", "-wt", path], check=False)
        result_text = result.stdout.decode() if result.stdout else ""
        if result.stderr:
            result_text += result.stderr.decode()
        result_text = _resolve_overstrikes(result_text)
        important_matches: list[str] = []
        for important_re in FLAC_IMPORTANT_REGEXES:
            important_matches.extend(m.strip() for m in important_re.findall(result_text))
        md5_unset = FLAC_MD5_UNSET_RE.search(result_text)
        # The warning line is the same fact md5_unset already carries; keeping both is
        # how one file produced two identical-looking lines.
        important_matches = [m for m in important_matches if not FLAC_MD5_UNSET_RE.search(m)]
        passed = result.returncode == 0 and not md5_unset
        name = os.path.basename(path)
        # -w makes the MD5 warning exit non-zero, so the return code alone cannot say
        # whether the audio decoded. flac saying "ok" can, and its absence fails closed.
        md5_only = bool(md5_unset) and bool(FLAC_OK_RE.search(result_text))
        return IntegrityResult(
            passed,
            "\n".join(important_matches),
            md5_unset=(name,) if md5_unset else (),
            decode_failures=() if passed or md5_only else (name,),
            checked=1,
        )
    except Exception:
        name = os.path.basename(path)
        return IntegrityResult(
            False,
            click.style(f"{name}: Failed integrity", fg="red", bold=True),
            decode_failures=(name,),
            checked=1,
        )


async def _check_mp3_integrity(path: str) -> IntegrityResult:
    """Check the integrity of a single MP3 file using mp3val.

    Args:
        path: Path to the MP3 file.

    Returns:
        An IntegrityResult built from the tool's output.
    """
    try:
        result = await anyio.run_process(["mp3val", path], check=False)
        result_text = result.stdout.decode() if result.stdout else ""
        name = os.path.basename(path)
        lines = [line.strip() for line in result_text.splitlines() if line.strip()]

        info = [line for line in lines if line.startswith(MP3_INFO_PREFIX)]
        errors = [line for line in lines if line.startswith(MP3_ERROR_PREFIX)]
        warnings = [line for line in lines if line.startswith(MP3_WARNING_PREFIX)]

        # mp3val exits 0 with the damage described in its output, so the text is
        # the verdict. An unreadable or empty file gets no prefix at all — just
        # "Cannot open input file" — and a run that produced no analysis proves
        # nothing, so it must not count as a pass either.
        passed = result.returncode == 0 and not errors and bool(info)
        described = info + errors
        details = "\n".join(f"{name}: {line}" for line in described) or f"{name}: {result_text.strip()}"
        # An mp3 has no MD5 state, so any failure here is a decode failure — without
        # this, a broken mp3 beside unset-MD5 flacs would ride their downgrade.
        return IntegrityResult(
            passed,
            details,
            tuple(f"{name}: {line}" for line in warnings),
            decode_failures=() if passed else (name,),
            checked=1,
        )
    except Exception:
        name = os.path.basename(path)
        return IntegrityResult(
            False,
            click.style(f"{name}: Failed integrity", fg="red", bold=True),
            decode_failures=(name,),
            checked=1,
        )


async def sanitize_integrity(path: str, _: int | None = None) -> bool:
    """Sanitize audio files by re-encoding to fix integrity issues.

    Args:
        path: Path to a FLAC/MP3 file or a directory containing audio files.
        _: Unused index parameter for process_files compatibility.

    Returns:
        True if all files sanitized successfully, False otherwise.

    Raises:
        click.Abort: If the path is neither a supported file nor a directory.
    """
    if path.lower().endswith(".flac"):
        return await _sanitize_flac(path)
    elif path.lower().endswith(".mp3"):
        return await _sanitize_mp3(path)
    elif os.path.isdir(path):
        integrities = True
        audio_files: list[str] = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in [".mp3", ".flac"]):
                    audio_files.append(os.path.join(root, f))
        if not audio_files:
            return True
        results = await process_files(audio_files, sanitize_integrity, "Sanitizing audio files")
        for integrity in results:
            integrities = integrities and integrity
        return integrities
    raise click.Abort


def _reserve_backup_path(path: str) -> str:
    """Atomically claim a .corrupted backup name (creates a placeholder file).

    O_EXCL makes the claim atomic, so a stale backup from a crashed run — or a
    concurrent claimer — can never be clobbered. The caller renames the real
    file over the placeholder, keeping the user-visible ``file.ext.corrupted``
    convention.
    """
    backup_path = path + ".corrupted"
    counter = 1
    while True:
        try:
            os.close(os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return backup_path
        except FileExistsError:
            backup_path = f"{path}.corrupted.{counter}"
            counter += 1


def _restore_backup(path: str, backup_path: str, moved: bool, *, clobber_partial: bool) -> None:
    """Put the original back after a failure; discard an unused placeholder.

    clobber_partial: overwrite a half-written output at ``path`` (flac re-encodes
    into ``path``; mp3val edits the backup in place, so mp3 must not clobber).
    """
    if not moved:
        with suppress(OSError):
            os.remove(backup_path)
        return
    if not os.path.exists(backup_path):
        return
    if os.path.exists(path) and not clobber_partial:
        return
    with suppress(OSError):
        os.replace(backup_path, path)


async def _sanitize_flac(path: str) -> bool:
    """Sanitize a FLAC file by re-encoding and cleaning metadata.

    Args:
        path: Path to the FLAC file.

    Returns:
        True if sanitization succeeded, False otherwise.
    """
    backup_path = _reserve_backup_path(path)
    moved = False
    try:
        # os.replace: on Windows os.rename raises FileExistsError over the claimed
        # placeholder; replace overwrites on both platforms.
        os.replace(path, backup_path)
        moved = True
        result = await anyio.run_process(
            ["flac", f"-{cfg.upload.compression.flac_compression_level}", backup_path, "-o", path],
            check=False,
        )
        if result.returncode != 0:
            stderr_text = result.stderr.decode() if result.stderr else ""
            stdout_text = result.stdout.decode() if result.stdout else ""
            raise Exception(f"FLAC encoding failed:\n{stdout_text}\n{stderr_text}")
        result = await anyio.run_process(
            ["metaflac", "--dont-use-padding", "--remove", "--block-type=PADDING,PICTURE", path],
            check=False,
        )
        if result.returncode != 0:
            raise Exception("Failed to remove FLAC metadata blocks")
        result = await anyio.run_process(
            ["metaflac", "--add-padding=8192", path],
            check=False,
        )
        if result.returncode != 0:
            raise Exception("Failed to add FLAC padding")
        os.remove(backup_path)  # only drop the backup once every step has succeeded
        return True
    except Exception as e:
        click.secho(f"Failed to sanitize {path}, {e}", fg="red", bold=True)
        # Restore the original if the re-encode failed with it renamed aside (#12).
        _restore_backup(path, backup_path, moved, clobber_partial=True)
        return False
    except BaseException:
        # Cancellation (webui cancel / Ctrl-C) is not an Exception — restore
        # the original before letting it propagate.
        _restore_backup(path, backup_path, moved, clobber_partial=True)
        raise


async def _sanitize_mp3(path: str) -> bool:
    """Sanitize an MP3 file using mp3val to fix structural issues.

    Args:
        path: Path to the MP3 file.

    Returns:
        True if sanitization succeeded, False otherwise.
    """
    backup_path = _reserve_backup_path(path)
    moved = False
    try:
        # os.replace: on Windows os.rename raises FileExistsError over the claimed
        # placeholder; replace overwrites on both platforms.
        os.replace(path, backup_path)
        moved = True

        result = await anyio.run_process(
            ["mp3val", "-f", "-si", "-nb", "-t", backup_path],
            check=False,
        )

        if os.path.exists(backup_path):
            os.rename(backup_path, path)

        # Check if the operation was successful
        if result.returncode == 0:
            return True
        else:
            # If mp3val failed, restore the original file
            if os.path.exists(backup_path) and not os.path.exists(path):
                os.rename(backup_path, path)
            stderr_text = result.stderr.decode() if result.stderr else ""
            raise Exception(f"mp3val failed with return code {result.returncode}: {stderr_text}")

    except Exception as e:
        click.secho(f"Failed to sanitize {path}, {e}", fg="red", bold=True)
        # Ensure we restore the original file if something went wrong.
        _restore_backup(path, backup_path, moved, clobber_partial=False)
        return False
    except BaseException:
        # Cancellation (webui cancel / Ctrl-C) is not an Exception — restore
        # the original before letting it propagate.
        _restore_backup(path, backup_path, moved, clobber_partial=False)
        raise
