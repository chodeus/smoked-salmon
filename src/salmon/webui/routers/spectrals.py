"""Spectral generation, gallery serving and image-host upload."""

import asyncio
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from salmon import cfg
from salmon.images import HOSTS, upload_images
from salmon.tagger.audio_info import gather_audio_info
from salmon.uploader.spectrals import create_specs_folder, generate_spectrals_all
from salmon.webui.jobs import Job, JobCapacityError, JobConflictError, manager
from salmon.webui.validation import validate_album_dir

router = APIRouter(tags=["spectrals"])


class GenerateRequest(BaseModel):
    path: str


class UploadRequest(BaseModel):
    job_id: str
    host: str | None = None


@router.post("/spectrals/generate")
async def generate(req: GenerateRequest) -> dict:
    path = validate_album_dir(req.path)

    async def run(job: Job) -> dict:
        audio_info = await asyncio.to_thread(gather_audio_info, path, True)
        spectrals_path = await asyncio.to_thread(create_specs_folder, path)
        spectral_ids = await generate_spectrals_all(path, spectrals_path, audio_info)
        files = sorted(f for f in os.listdir(spectrals_path) if f.lower().endswith(".png"))
        return {
            "album_path": path,
            "spectrals_path": spectrals_path,
            "spectral_ids": {str(k): v for k, v in spectral_ids.items()},
            "files": files,
        }

    title = f"Spectrals: {os.path.basename(path)}"
    try:
        job = manager.create_threaded("spectrals", title, run, {"path": path}, lock_key=path)
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()


@router.get("/spectrals/{job_id}/image/{filename}")
def image(job_id: str, filename: str) -> FileResponse:
    job = manager.jobs.get(job_id)
    if job is None or job.type != "spectrals" or job.status != "done":
        raise HTTPException(status_code=404, detail="No finished spectrals job with this id.")
    if filename not in job.result["files"]:
        raise HTTPException(status_code=404, detail="Unknown spectral image.")
    return FileResponse(os.path.join(job.result["spectrals_path"], filename))


@router.post("/spectrals/upload")
async def upload(req: UploadRequest) -> dict:
    source_job = manager.jobs.get(req.job_id)
    if source_job is None or source_job.type != "spectrals" or source_job.status != "done":
        raise HTTPException(status_code=404, detail="No finished spectrals job with this id.")
    host_name = req.host or cfg.image.specs_uploader
    host = HOSTS.get(host_name)
    if host is None:
        raise HTTPException(status_code=422, detail=f"Unknown image host: {host_name}")

    spectrals_path = source_job.result["spectrals_path"]
    files = [os.path.join(spectrals_path, f) for f in source_job.result["files"]]

    async def run(job: Job) -> dict:
        urls = await upload_images(files, host)
        return {"host": host_name, "urls": urls}

    title = f"Upload spectrals: {os.path.basename(source_job.result['album_path'])}"
    try:
        job = manager.create_threaded(
            "spectrals-upload", title, run, {"job_id": req.job_id, "host": host_name},
            lock_key=f"spectrals-upload:{req.job_id}",
        )
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return job.to_dict()
