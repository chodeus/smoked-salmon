"""Transcoding and downconversion jobs."""

import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from salmon.converter.downconverting import convert_folder
from salmon.converter.transcoding import transcode_folder
from salmon.webui.jobs import Job, JobConflictError, manager

router = APIRouter(tags=["convert"])


class TranscodeRequest(BaseModel):
    path: str
    bitrate: Literal["V0", "320"]


class DownconvertRequest(BaseModel):
    path: str


def _validate_dir(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail=f"Not a directory: {path}")
    return path


@router.post("/convert/transcode")
async def transcode(req: TranscodeRequest) -> dict:
    path = _validate_dir(req.path)

    async def run(job: Job) -> dict:
        output = await transcode_folder(path, req.bitrate)
        return {"output_path": output}

    title = f"Transcode {req.bitrate}: {os.path.basename(path)}"
    try:
        job = manager.create("transcode", title, run, {"path": path, "bitrate": req.bitrate}, lock_key=path)
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return job.to_dict()


@router.post("/convert/downconvert")
async def downconvert(req: DownconvertRequest) -> dict:
    path = _validate_dir(req.path)

    async def run(job: Job) -> dict:
        _sample_rate, output = await convert_folder(path)
        return {"output_path": output}

    title = f"Downconvert: {os.path.basename(path)}"
    try:
        job = manager.create("downconvert", title, run, {"path": path}, lock_key=path)
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return job.to_dict()
