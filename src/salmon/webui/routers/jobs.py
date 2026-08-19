"""Job listing, inspection, questions, cancellation and the live event websocket."""

import asyncio
import os
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from salmon.webui.jobs import manager

router = APIRouter(tags=["jobs"])

ALLOWED_WS_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def _origin_allowed(origin: str | None) -> bool:
    """Reject cross-site websocket connections (any non-localhost Origin)."""
    if not origin:
        return True  # non-browser clients (curl, scripts) send no Origin
    return urlparse(origin).hostname in ALLOWED_WS_HOSTS


@router.get("/jobs")
async def list_jobs() -> list[dict]:
    return [job.to_dict() for job in reversed(manager.jobs.values())]


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job.")
    return job.to_dict()


class AnswerRequest(BaseModel):
    question_id: str
    value: str | int | float | bool | None = None


@router.post("/jobs/{job_id}/answer")
async def answer_question(job_id: str, req: AnswerRequest) -> dict:
    if job_id not in manager.jobs:
        raise HTTPException(status_code=404, detail="Unknown job.")
    if not manager.answer(job_id, req.question_id, req.value):
        raise HTTPException(status_code=409, detail="No matching pending question (already answered?).")
    return {"answered": req.question_id}


@router.get("/jobs/{job_id}/spectral/{filename}")
async def job_spectral(job_id: str, filename: str) -> FileResponse:
    job = manager.jobs.get(job_id)
    if job is None or job.interaction is None or job.interaction.spectrals is None:
        raise HTTPException(status_code=404, detail="No spectrals for this job.")
    spectrals = job.interaction.spectrals
    if filename not in spectrals["files"]:
        raise HTTPException(status_code=404, detail="Unknown spectral image.")
    return FileResponse(os.path.join(spectrals["path"], filename))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    if not manager.cancel(job_id):
        raise HTTPException(status_code=409, detail="Job not found or already finished.")
    return {"cancelled": job_id}


@router.websocket("/ws")
async def events(websocket: WebSocket) -> None:
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    queue = manager.subscribe()
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        manager.unsubscribe(queue)
