"""Job listing, inspection, cancellation and the live event websocket."""

import asyncio
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

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
