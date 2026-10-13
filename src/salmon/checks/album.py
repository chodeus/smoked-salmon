"""Run salmon's quality checks across a whole album folder, returning plain data.

The checks themselves live in their own modules and are shared with the CLI upload
path; these wrappers only widen them from one file to a directory and drop the
result into JSON-able shapes.
"""

import asyncio
import os

import asyncclick as click
import cambia
import msgspec
from mutagen import MutagenError

from salmon.checks.integrity import check_integrity
from salmon.checks.logs import log_checksum, log_score, parse_log, verify_log_crcs
from salmon.checks.mqa import check_mqa
from salmon.checks.provenance import gather_provenance
from salmon.checks.tag_rules import has_blank_id3v2_alongside_id3v1
from salmon.checks.upconverts import check_upconvert
from salmon.common.files import get_audio_files, get_flac_files
from salmon.common.progress import report_progress
from salmon.errors import CRCMismatchError, LogCheckSkipped, UpconvertCheckNotApplicable


def _log_paths(path: str) -> list[str]:
    return sorted(
        os.path.join(root, name)
        for root, _dirs, files in os.walk(path)
        for name in files
        if name.lower().endswith(".log")
    )


def _in_album(text: str, path: str) -> str:
    """text, naming files from the album folder instead of from its parent."""
    return text.replace(os.path.dirname(os.path.normpath(path)) + os.sep, "")


async def _log(logpath: str, path: str) -> dict:
    """One rip log's score, checksum and CRCs, read as salmon up checks them."""
    name = os.path.relpath(logpath, path)
    try:
        output = await asyncio.to_thread(parse_log, logpath)
    except (LogCheckSkipped, OSError) as e:
        return {"file": name, "error": str(e).replace(logpath, name)}
    checksum = log_checksum(output)
    result = {"file": name, "score": log_score(output), "checksum_integrity": checksum.name}
    if checksum == cambia.Integrity.Mismatch:
        # salmon up stops at an edited log before reading its CRCs.
        return result | {"crcs": "skipped", "crcs_detail": "the log was edited"}
    try:
        await verify_log_crcs(output, logpath, path)
    except CRCMismatchError:
        return result | {"crcs": "mismatch", "crcs_detail": ""}
    except LogCheckSkipped as e:
        return result | {"crcs": "skipped", "crcs_detail": _in_album(str(e), path)}
    except Exception as e:
        return result | {"crcs": "error", "crcs_detail": _in_album(str(e), path)}
    return result | {"crcs": "match", "crcs_detail": ""}


async def run_log_check(path: str) -> dict:
    return {"logs": [await _log(logpath, path) for logpath in _log_paths(path)]}


def _mp3_tags(path: str) -> dict:
    mp3s = [name for name in get_audio_files(path) if name.lower().endswith(".mp3")]
    dual, errors = [], []
    for name in mp3s:
        try:
            if has_blank_id3v2_alongside_id3v1(os.path.join(path, name)):
                dual.append(name)
        except (MutagenError, OSError) as e:
            errors.append({"file": name, "error": str(e)})
    return {"mp3s": len(mp3s), "dual_id3": dual, "errors": errors}


async def run_tag_check(path: str) -> dict:
    """The MP3s whose filled-in ID3v1 tag sits beside a blank ID3v2 one, as salmon up notes them."""
    return await asyncio.to_thread(_mp3_tags, path)


async def run_provenance_check(path: str) -> dict:
    return await asyncio.to_thread(gather_provenance, path)


async def run_integrity_check(path: str) -> dict:
    result = await check_integrity(path)
    return {
        "passed": result.passed,
        "details": click.unstyle(result.details),
        "concerns": [click.unstyle(c) for c in result.concerns],
        "md5_unset": list(result.md5_unset),
        "decode_failures": list(result.decode_failures),
        "checked": result.checked,
    }


# flac and sox are spawned per file; keep a lid on how many run at once.
_MAX_CONCURRENT_FILE_CHECKS = 4


async def _map_files(files: list[str], worker, desc: str) -> list[dict]:
    """Run worker over every file concurrently, in order, reporting progress."""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FILE_CHECKS)
    done = 0

    async def run_one(filename: str) -> dict:
        nonlocal done
        async with semaphore:
            result = await worker(filename)
        done += 1
        report_progress(done, len(files), f"{desc} ({filename})")
        return result

    return list(await asyncio.gather(*(run_one(f) for f in files)))


async def run_mqa_check(path: str) -> dict:
    async def one(f: str) -> dict:
        return {"file": f, "detected": await check_mqa(os.path.join(path, f))}

    results = await _map_files(get_audio_files(path, True), one, "Checking for MQA")
    return {"detected": any(r["detected"] for r in results), "files": results}


async def run_upconvert_check(path: str) -> dict:
    async def one(f: str) -> dict:
        try:
            return msgspec.to_builtins(await check_upconvert(os.path.join(path, f))) | {"file": f}
        except UpconvertCheckNotApplicable as e:
            # Out of scope (16bit), not a failure — kept distinct so it reads as "skipped".
            return {"file": f, "not_applicable": str(e)}
        except Exception as e:
            return {"file": f, "error": str(e)}

    flacs = get_flac_files(path, True)
    return {"files": await _map_files(flacs, one, "Checking for upconversion")}
