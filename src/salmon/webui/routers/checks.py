"""Album quality checks: rip logs, integrity, MQA, upconversion, duplicates."""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import salmon.trackers
from salmon.checks.preflight import CHECK_IDS, run_checks
from salmon.constants import SOURCES
from salmon.webui.jobs import Job, JobCapacityError, manager
from salmon.webui.validation import validate_album_dir

router = APIRouter(tags=["checks"])


class ChecksRequest(BaseModel):
    path: str
    checks: list[str] | None = None
    source: str | None = None
    trackers: list[str] = []


@router.post("/checks/run")
async def run(req: ChecksRequest) -> dict:
    """Verify an album. With trackers, this is the pre-upload check; without, the files alone."""
    path = validate_album_dir(req.path)
    if req.checks is not None:
        invalid = [c for c in req.checks if c not in CHECK_IDS]
        if invalid:
            raise HTTPException(status_code=422, detail=f"Invalid checks: {invalid}")
    if req.source and req.source not in SOURCES.values():
        raise HTTPException(status_code=422, detail=f"Invalid source: {req.source}")
    unknown = [t for t in req.trackers if t not in salmon.trackers.tracker_list]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown tracker(s): {', '.join(unknown)}")

    async def job_body(job: Job) -> dict:
        return await run_checks(path, req.checks, req.source, req.trackers)

    selected = ", ".join(CHECK_IDS if req.checks is None else req.checks) or "no file checks"
    try:
        job = manager.create_threaded(
            "checks",
            f"Checks ({selected}): {os.path.basename(path)}",
            job_body,
            {"path": path, "checks": req.checks, "trackers": req.trackers},
        )
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()
