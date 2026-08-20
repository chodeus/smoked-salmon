"""Quality-check jobs: rip logs, integrity, MQA, upconvert detection, pre-upload verdicts."""

import asyncio
import os

import asyncclick as click
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import salmon.trackers
from salmon.checks.integrity import check_integrity
from salmon.checks.preflight import run_log_check, run_mqa_check, run_preflight, run_upconvert_check
from salmon.constants import SOURCES
from salmon.webui.jobs import Job, JobCapacityError, manager
from salmon.webui.validation import validate_album_dir

router = APIRouter(tags=["checks"])

CHECK_TYPES = ("log", "integrity", "mqa", "upconvert")


class ChecksRequest(BaseModel):
    path: str
    checks: list[str]


class PreflightRequest(BaseModel):
    path: str
    source: str | None = None
    trackers: list[str] = []
    skip_log_check: bool = False
    skip_integrity_check: bool = False
    skip_mqa: bool = False
    skip_up: bool = False


@router.post("/checks/run")
async def run_checks(req: ChecksRequest) -> dict:
    path = validate_album_dir(req.path)
    invalid = [c for c in req.checks if c not in CHECK_TYPES]
    if invalid or not req.checks:
        raise HTTPException(status_code=422, detail=f"Invalid checks: {invalid or 'none selected'}")

    async def run(job: Job) -> dict:
        results: dict[str, dict] = {}
        if "log" in req.checks:
            results["log"] = await asyncio.to_thread(run_log_check, path)
        if "integrity" in req.checks:
            passed, details = await check_integrity(path)
            results["integrity"] = {"passed": passed, "details": click.unstyle(details)}
        if "mqa" in req.checks:
            results["mqa"] = await run_mqa_check(path)
        if "upconvert" in req.checks:
            results["upconvert"] = await run_upconvert_check(path)
        return results

    title = f"Checks ({', '.join(req.checks)}): {os.path.basename(path)}"
    try:
        job = manager.create_threaded("checks", title, run, {"path": path, "checks": req.checks})
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()


@router.post("/checks/preflight")
async def preflight(req: PreflightRequest) -> dict:
    """Verify an album against every check before anything is staged or uploaded."""
    path = validate_album_dir(req.path)
    if req.source and req.source not in SOURCES.values():
        raise HTTPException(status_code=422, detail=f"Invalid source: {req.source}")
    unknown = [t for t in req.trackers if t not in salmon.trackers.tracker_list]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown tracker(s): {', '.join(unknown)}")

    skips = {
        "log": req.skip_log_check,
        "integrity": req.skip_integrity_check,
        "mqa": req.skip_mqa,
        "upconvert": req.skip_up,
    }

    async def run(job: Job) -> dict:
        return await run_preflight(path, req.source, req.trackers, skips)

    try:
        job = manager.create_threaded(
            "preflight", f"Pre-flight: {os.path.basename(path)}", run, {"path": path, "trackers": req.trackers}
        )
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()
