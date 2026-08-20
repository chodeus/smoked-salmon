"""The interactive upload wizard: runs the unmodified upload pipeline as a
thread job; terminal prompts surface as browser questions via the jobs API."""

import os

import asyncclick as click
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import salmon.trackers
from salmon.constants import SOURCES as SOURCE_CODES
from salmon.constants import TAG_ENCODINGS
from salmon.uploader import upload as run_upload
from salmon.uploader.preassumptions import confirm_group_upload, print_preassumptions
from salmon.webui.jobs import Job, JobCapacityError, JobConflictError, manager
from salmon.webui.validation import validate_album_dir

router = APIRouter(tags=["upload"])

# Derived, never hand-maintained: the copy this replaced had drifted to offer
# "Blu-Ray", which the tagger then rejects mid-job as an invalid source.
SOURCES = list(SOURCE_CODES.values())


class UploadStartRequest(BaseModel):
    path: str
    tracker: str
    source: str | None = None
    group_id: int | None = None
    request: str | None = None
    source_url: str | None = None
    lossy: bool | None = None
    spectrals_after: bool = False
    auto_rename: bool = False
    compress: bool = False
    scene: bool = False
    skip_up: bool = False
    skip_mqa: bool = False
    skip_log_check: bool = False
    skip_integrity_check: bool = False
    essential_only: bool = False
    dry_run: bool = False
    overwrite: bool = False
    encoding: str | None = None
    spectrals: list[int] = Field(default_factory=list)
    skip_initial_review: bool = False
    apply_ai_suggestions: bool = False


@router.get("/upload/options")
async def options() -> dict:
    return {
        "trackers": salmon.trackers.tracker_list,
        "sources": SOURCES,
        "encodings": list(TAG_ENCODINGS),
    }


@router.post("/upload")
async def start(req: UploadStartRequest) -> dict:
    path = validate_album_dir(req.path)
    if req.tracker not in salmon.trackers.tracker_list:
        raise HTTPException(status_code=422, detail=f"Unknown tracker: {req.tracker}")
    if req.source is not None and req.source not in SOURCES:
        raise HTTPException(status_code=422, detail=f"Unknown source: {req.source}")
    if req.encoding is not None and req.encoding not in TAG_ENCODINGS:
        raise HTTPException(status_code=422, detail=f"Unknown encoding: {req.encoding}")
    # Mirrors the CLI, where these two are mutually exclusive.
    if req.essential_only and req.scene:
        raise HTTPException(status_code=422, detail="essential_only and scene cannot be combined.")

    request_id = req.request
    if request_id:
        try:
            request_id = salmon.trackers.validate_request(salmon.trackers.get_class(req.tracker)(), request_id)
        except click.ClickException as e:
            raise HTTPException(status_code=422, detail=e.message) from e

    async def run(job: Job) -> dict:
        gazelle_site = salmon.trackers.get_class(req.tracker)()
        gazelle_site.dry_run = req.dry_run
        spectrals = tuple(req.spectrals)
        print_preassumptions(
            gazelle_site, path, req.group_id, req.source, req.lossy, spectrals, req.encoding, req.spectrals_after
        )
        if req.group_id:
            await confirm_group_upload(gazelle_site, req.group_id, req.source)
        await run_upload(
            gazelle_site,
            path,
            req.group_id,
            req.source,
            req.lossy,
            spectrals,
            req.encoding,
            scene=req.scene,
            overwrite_meta=req.overwrite,
            recompress=req.compress,
            source_url=req.source_url.strip() if req.source_url else None,
            request_id=request_id,
            spectrals_after=req.spectrals_after,
            auto_rename=req.auto_rename,
            skip_up=req.skip_up,
            skip_mqa=req.skip_mqa,
            skip_log_check=req.skip_log_check,
            skip_integrity_check=req.skip_integrity_check,
            essential_only=req.essential_only,
            skip_initial_review=req.skip_initial_review,
            apply_ai_suggestions=req.apply_ai_suggestions,
        )
        return {"album_path": path, "tracker": req.tracker, "dry_run": req.dry_run}

    title = f"Upload to {req.tracker}: {os.path.basename(path)}"
    try:
        job = manager.create_threaded("upload", title, run, req.model_dump() | {"path": path}, lock_key=path)
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()
