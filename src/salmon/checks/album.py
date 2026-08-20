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
from salmon.checks.upconverts import check_upconvert
from salmon.common.files import get_audio_files


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
                        "checksum_integrity": str(integrity).rsplit(".", 1)[-1].rstrip(">"),
                    }
                )
            except Exception as e:
                logs.append({"file": os.path.relpath(logpath, path), "error": str(e)})
    return {"logs": logs}


async def run_log_check(path: str) -> dict:
    return await asyncio.to_thread(_parse_logs, path)


async def run_integrity_check(path: str) -> dict:
    passed, details = await check_integrity(path)
    return {"passed": passed, "details": click.unstyle(details)}


async def run_mqa_check(path: str) -> dict:
    results = []
    detected = False
    for f in get_audio_files(path, True):
        found = await check_mqa(os.path.join(path, f))
        detected = detected or found
        results.append({"file": f, "detected": found})
    return {"detected": detected, "files": results}


async def run_upconvert_check(path: str) -> dict:
    results = []
    for f in get_audio_files(path, True):
        if not f.lower().endswith(".flac"):
            continue
        try:
            result = await check_upconvert(os.path.join(path, f))
            results.append(msgspec.to_builtins(result) | {"file": f})
        except Exception as e:
            results.append({"file": f, "error": str(e)})
    return {"files": results}
