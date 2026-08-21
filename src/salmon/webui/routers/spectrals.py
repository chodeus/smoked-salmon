"""Spectral generation, gallery serving and image-host upload."""

import asyncio
import os
import shutil

import msgspec
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from salmon import cfg
from salmon.checks.provenance import gather_provenance
from salmon.checks.report import build_report
from salmon.common.files import get_audio_files
from salmon.images import HOSTS, upload_images
from salmon.tagger.audio_info import gather_audio_info
from salmon.uploader.frequency import assess, generate_frequency_plots
from salmon.uploader.spectrals import create_specs_folder, generate_spectrals_all, get_spectrals_path
from salmon.webui.jobs import Job, JobCapacityError, JobConflictError, manager
from salmon.webui.validation import allowed_roots, is_within_roots, refuse_library_output, validate_album_dir

router = APIRouter(tags=["spectrals"])


class GenerateRequest(BaseModel):
    path: str


class UploadRequest(BaseModel):
    job_id: str
    host: str | None = None


def _discard_spectrals(job: Job) -> None:
    """Remove a finished job's spectrals folder.

    Re-confines the resolved path immediately before the delete: the path was
    vetted when the job was created, but this runs much later and rmtree is not
    a call to make on a path that has since become a symlink somewhere else.
    """
    path = (job.result or {}).get("spectrals_path")
    if not path:
        return
    real = os.path.realpath(path)
    roots = allowed_roots()
    if not is_within_roots(real, roots) or real in roots or cfg.directory.is_library_path(real):
        return
    shutil.rmtree(real, ignore_errors=True)


@router.post("/spectrals/generate")
async def generate(req: GenerateRequest) -> dict:
    path = validate_album_dir(req.path)
    # Spectrals go to tmp_dir, so a library album is fine; only the no-tmp_dir
    # fallback writes into the album, and that is what must be refused.
    spectrals_path = get_spectrals_path(path)
    refuse_library_output(spectrals_path, "Spectrals")

    async def run(job: Job) -> dict:
        audio_info = await asyncio.to_thread(gather_audio_info, path, True)
        created = await asyncio.to_thread(create_specs_folder, path, spectrals_path)
        spectral_ids = await generate_spectrals_all(path, created, audio_info)
        spectra = await generate_frequency_plots(path, get_audio_files(path, True), created)
        provenance = await asyncio.to_thread(gather_provenance, path)
        return {
            "album_path": path,
            "spectrals_path": created,
            "spectral_ids": {str(k): v for k, v in spectral_ids.items()},
            "files": sorted(f for f in os.listdir(created) if f.lower().endswith(".png")),
            "frequency": [msgspec.to_builtins(s) for s in spectra],
            "assessment": assess(spectra),
            "provenance": provenance,
            "report": build_report(path, audio_info, provenance, spectra),
        }

    title = f"Spectrals: {os.path.basename(path)}"
    try:
        job = manager.create_threaded("spectrals", title, run, {"path": path}, lock_key=path)
    except JobConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobCapacityError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    job.on_evict = _discard_spectrals
    return job.to_dict()


def _finished_spectrals_job(job_id: str) -> Job:
    job = manager.jobs.get(job_id)
    if job is None or job.type != "spectrals" or job.status != "done":
        raise HTTPException(status_code=404, detail="No finished spectrals job with this id.")
    return job


@router.get("/spectrals/{job_id}/image/{filename}")
def image(job_id: str, filename: str) -> FileResponse:
    job = _finished_spectrals_job(job_id)
    if filename not in job.result["files"]:
        raise HTTPException(status_code=404, detail="Unknown spectral image.")
    return FileResponse(os.path.join(job.result["spectrals_path"], filename))


@router.delete("/spectrals/{job_id}")
def discard(job_id: str) -> dict:
    """Delete the generated images once they are no longer wanted."""
    job = _finished_spectrals_job(job_id)
    _discard_spectrals(job)
    job.result = {**job.result, "files": [], "frequency": [], "discarded": True}
    return {"discarded": True}


@router.post("/spectrals/upload")
async def upload(req: UploadRequest) -> dict:
    source_job = _finished_spectrals_job(req.job_id)
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
