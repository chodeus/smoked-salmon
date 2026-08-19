"""Quality-check jobs: rip logs, integrity, MQA, upconvert detection."""

import asyncio
import os

import asyncclick as click
import cambia
import msgspec
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from salmon.checks.integrity import check_integrity
from salmon.checks.mqa import check_mqa
from salmon.checks.upconverts import check_upconvert
from salmon.common.files import get_audio_files
from salmon.webui.jobs import Job, JobCapacityError, manager
from salmon.webui.validation import validate_album_dir

router = APIRouter(tags=["checks"])

CHECK_TYPES = ("log", "integrity", "mqa", "upconvert")


class ChecksRequest(BaseModel):
    path: str
    checks: list[str]


def _check_logs(path: str) -> dict:
    logs = []
    for root, _dirs, files in os.walk(path):
        for f in sorted(files):
            if f.lower().endswith(".log"):
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


async def _check_mqa_files(path: str) -> dict:
    results = []
    detected = False
    for f in get_audio_files(path, True):
        found = await check_mqa(os.path.join(path, f))
        detected = detected or found
        results.append({"file": f, "detected": found})
    return {"detected": detected, "files": results}


async def _check_upconverts(path: str) -> dict:
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


@router.post("/checks/run")
async def run_checks(req: ChecksRequest) -> dict:
    path = validate_album_dir(req.path)
    invalid = [c for c in req.checks if c not in CHECK_TYPES]
    if invalid or not req.checks:
        raise HTTPException(status_code=422, detail=f"Invalid checks: {invalid or 'none selected'}")

    async def run(job: Job) -> dict:
        results: dict[str, dict] = {}
        if "log" in req.checks:
            results["log"] = await asyncio.to_thread(_check_logs, path)
        if "integrity" in req.checks:
            passed, details = await check_integrity(path)
            results["integrity"] = {"passed": passed, "details": click.unstyle(details)}
        if "mqa" in req.checks:
            results["mqa"] = await _check_mqa_files(path)
        if "upconvert" in req.checks:
            results["upconvert"] = await _check_upconverts(path)
        return results

    title = f"Checks ({', '.join(req.checks)}): {os.path.basename(path)}"
    try:
        job = manager.create_threaded("checks", title, run, {"path": path, "checks": req.checks})
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()
