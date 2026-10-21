"""Job listing, inspection, questions, cancellation and the live event websocket."""

import asyncio
import os

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from salmon.webui.auth import still_authenticated
from salmon.webui.jobs import manager

router = APIRouter(tags=["jobs"])

# An idle stream checks this often that its login still holds, so an ended one frees its place.
SESSION_RECHECK_SECONDS = 60


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


@router.get("/jobs/{job_id}/spectrals/{filename}")
async def job_spectral(job_id: str, filename: str) -> FileResponse:
    job = manager.jobs.get(job_id)
    if job is None or job.asker is None or job.asker.spectrals is None:
        raise HTTPException(status_code=404, detail="No spectrals for this job.")
    spectrals = job.asker.spectrals
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
    await websocket.accept()
    queue = manager.subscribe()
    if queue is None:  # too many live connections
        await websocket.close(code=1013)
        return
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), SESSION_RECHECK_SECONDS)
            except TimeoutError:
                event = None
            # A stream opened with a session gets nothing once a logout has ended it.
            if not still_authenticated(websocket):
                await websocket.close(code=1008)
                return
            if event is not None:
                await websocket.send_json(event)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        manager.unsubscribe(queue)
