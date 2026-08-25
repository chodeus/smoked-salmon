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

from salmon.checks.integrity import check_integrity
from salmon.checks.mqa import check_mqa
from salmon.checks.provenance import gather_provenance
from salmon.checks.upconverts import check_upconvert
from salmon.common.files import get_audio_files
from salmon.common.progress import report_progress
from salmon.errors import UpconvertCheckNotApplicable


def _parse_logs(path: str) -> dict:
    logs = []
    for root, _dirs, files in os.walk(path):
        for f in sorted(files):
            if not f.lower().endswith(".log"):
                continue
            logpath = os.path.join(root, f)
            try:
                output = cambia.parse_log_file(logpath)
                score = int(output.evaluation_combined[0].combined_score)
                integrity = output.parsed.parsed_logs[0].checksum.integrity
                logs.append(
                    {
                        "file": os.path.relpath(logpath, path),
                        "score": score,
                        "checksum_integrity": getattr(integrity, "name", None)
                        or str(integrity).rsplit(".", 1)[-1].rstrip(">"),
                    }
                )
            except Exception as e:
                logs.append({"file": os.path.relpath(logpath, path), "error": str(e)})
    return {"logs": logs}


async def run_log_check(path: str) -> dict:
    return await asyncio.to_thread(_parse_logs, path)


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

    flacs = [f for f in get_audio_files(path, True) if f.lower().endswith(".flac")]
    return {"files": await _map_files(flacs, one, "Checking for upconversion")}
