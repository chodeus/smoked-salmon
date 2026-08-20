"""Transcoding and downconversion jobs."""

import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from salmon.common import compress as recompress
from salmon.converter.downconverting import convert_folder
from salmon.converter.transcoding import transcode_folder
from salmon.webui.jobs import Job, JobCapacityError, JobConflictError, manager
from salmon.webui.validation import validate_writable_album_dir

router = APIRouter(tags=["convert"])


class TranscodeRequest(BaseModel):
    path: str
    bitrate: Literal["V0", "320"]


class DownconvertRequest(BaseModel):
    path: str


class CompressRequest(BaseModel):
    path: str


@router.post("/convert/transcode")
async def transcode(req: TranscodeRequest) -> dict:
    path = validate_writable_album_dir(req.path)

    async def run(job: Job) -> dict:
        output = await transcode_folder(path, req.bitrate)
        return {"output_path": output}

    title = f"Transcode {req.bitrate}: {os.path.basename(path)}"
    try:
        job = manager.create_threaded("transcode", title, run, {"path": path, "bitrate": req.bitrate}, lock_key=path)
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()


@router.post("/convert/downconvert")
async def downconvert(req: DownconvertRequest) -> dict:
    path = validate_writable_album_dir(req.path)

    async def run(job: Job) -> dict:
        _sample_rate, output = await convert_folder(path)
        return {"output_path": output}

    title = f"Downconvert: {os.path.basename(path)}"
    try:
        job = manager.create_threaded("downconvert", title, run, {"path": path}, lock_key=path)
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()


@router.post("/convert/compress")
async def compress(req: CompressRequest) -> dict:
    """Recompress a folder's FLACs to the configured compression level, in place."""
    path = validate_writable_album_dir(req.path)

    async def run(job: Job) -> dict:
        count = 0
        for root, _dirs, files in os.walk(path):
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() == ".flac":
                    await recompress(os.path.join(root, name))
                    count += 1
        return {"path": path, "recompressed": count}

    title = f"Recompress: {os.path.basename(path)}"
    try:
        job = manager.create_threaded("compress", title, run, {"path": path}, lock_key=path)
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()
