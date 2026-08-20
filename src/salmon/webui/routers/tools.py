"""Endpoints for the remaining CLI commands: descgen, images, tag and cross-upload.

`tag` and `cross_upload` are click commands, so their bodies are reached through
`.callback`. Both are interactive; the prompt bridge turns their questions into
browser questions the same way an upload job does.
"""

import os
from typing import Any, cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import salmon.trackers
from salmon import cfg
from salmon.constants import TAG_ENCODINGS
from salmon.cross_upload import cross_upload as cross_upload_command
from salmon.images import HOSTS, upload_images
from salmon.tagger import tag as tag_command
from salmon.uploader.description import build_tracklist_description
from salmon.webui.jobs import Job, JobCapacityError, JobConflictError, manager
from salmon.webui.routers.upload import SOURCES
from salmon.webui.validation import is_within_roots, validate_album_dir

router = APIRouter(tags=["tools"])

# click stores each command's coroutine on .callback, typed Optional. Narrow once here
# so the endpoints below read plainly and a packaging mistake fails at import, not mid-job.
if tag_command.callback is None or cross_upload_command.callback is None:  # pragma: no cover
    raise RuntimeError("salmon CLI commands are missing their callbacks")
_TAG = cast("Any", tag_command.callback)
_CROSS_UPLOAD = cast("Any", cross_upload_command.callback)


class DescgenRequest(BaseModel):
    urls: list[str] = Field(min_length=1)


class ImageUploadRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    host: str | None = None


class TagRequest(BaseModel):
    path: str
    source: str
    encoding: str | None = None
    overwrite: bool = False
    auto_rename: bool = False
    skip_initial_review: bool = False
    apply_ai_suggestions: bool = False


class CrossUploadRequest(BaseModel):
    path: str
    source: str
    target: str
    downconvert: bool = False
    target_group_id: int | None = None
    all_formats: bool = False
    transcodes: list[str] = Field(default_factory=list)


def _queue(kind: str, title: str, run, meta: dict, lock_key: str | None = None) -> dict:
    try:
        job = manager.create_threaded(kind, title, run, meta, lock_key=lock_key)
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()


@router.get("/tools/options")
async def options() -> dict:
    return {
        "trackers": salmon.trackers.tracker_list,
        "sources": SOURCES,
        "encodings": list(TAG_ENCODINGS),
        "image_hosts": sorted(HOSTS),
        "transcodes": ["320", "V0"],
    }


@router.post("/descgen")
async def descgen(req: DescgenRequest) -> dict:
    """Build a tracklist description from one or more metadata URLs."""
    try:
        description = await build_tracklist_description(req.urls)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not build a description: {e}") from e
    return {"description": description}


@router.post("/images/upload")
async def images_upload(req: ImageUploadRequest) -> dict:
    """Upload image files to an image host and return their URLs."""
    host_name = req.host or cfg.image.image_uploader
    if host_name not in HOSTS:
        raise HTTPException(status_code=422, detail=f"Unknown image host: {host_name}")

    resolved = []
    for raw in req.paths:
        path = os.path.realpath(os.path.expanduser(raw))
        # Same confinement as album jobs: never read arbitrary files off the host.
        if not is_within_roots(path):
            raise HTTPException(status_code=403, detail="Refusing to read outside the configured directories.")
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail=f"Not a file: {raw}")
        resolved.append(path)

    try:
        urls = await upload_images(resolved, HOSTS[host_name])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Image upload failed: {e}") from e
    return {"host": host_name, "urls": urls}


@router.post("/tag")
async def tag(req: TagRequest) -> dict:
    """Interactively retag an album; prompts surface as browser questions."""
    path = validate_album_dir(req.path)
    if req.source not in SOURCES:
        raise HTTPException(status_code=422, detail=f"Unknown source: {req.source}")
    if req.encoding is not None and req.encoding not in TAG_ENCODINGS:
        raise HTTPException(status_code=422, detail=f"Unknown encoding: {req.encoding}")

    async def run(job: Job) -> dict:
        await _TAG(
            path=path,
            source=req.source,
            encoding=req.encoding,
            overwrite=req.overwrite,
            auto_rename=req.auto_rename,
            skip_initial_review=req.skip_initial_review,
            apply_ai_suggestions=req.apply_ai_suggestions,
        )
        return {"path": path}

    return _queue("tag", f"Tag: {os.path.basename(path)}", run, {"path": path}, lock_key=path)


@router.post("/cross-upload")
async def cross_upload(req: CrossUploadRequest) -> dict:
    """Copy an existing upload from one tracker to another."""
    for code in (req.source, req.target):
        if code not in salmon.trackers.tracker_list:
            raise HTTPException(status_code=422, detail=f"Unknown tracker: {code}")
    if req.source == req.target:
        raise HTTPException(status_code=422, detail="Source and target trackers must differ.")
    for bitrate in req.transcodes:
        if bitrate not in ("320", "V0"):
            raise HTTPException(status_code=422, detail=f"Unknown transcode: {bitrate}")

    async def run(job: Job) -> dict:
        await _CROSS_UPLOAD(
            torrent_or_directory=req.path,
            source=req.source,
            target=req.target,
            downconvert=req.downconvert,
            target_group_id=req.target_group_id,
            all_formats=req.all_formats,
            transcodes=tuple(req.transcodes),
        )
        return {"source": req.source, "target": req.target}

    title = f"Cross-upload {req.source} -> {req.target}"
    return _queue("cross-upload", title, run, req.model_dump())
