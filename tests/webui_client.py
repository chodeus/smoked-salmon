"""A TestClient for salmon web that sends the token and JSON, as the browser does once logged in."""

from typing import Any

from fastapi.testclient import TestClient

from salmon.webui.app import create_app
from salmon.webui.jobs import manager

WEB_TOKEN = "test-web-token-0001"


def web_client(base_url: str = "http://localhost", **app_options: Any) -> TestClient:
    headers = {"Authorization": f"Bearer {WEB_TOKEN}", "Content-Type": "application/json"}
    return TestClient(create_app(auth_token=WEB_TOKEN, **app_options), base_url=base_url, headers=headers)


def reset_manager() -> None:
    """Empty the job manager the routes use, its jobs cancelled and their threads ended first: the tests share it."""
    manager._subscribers.clear()
    for job in list(manager.jobs.values()):
        if job.finished_at is None:
            manager.cancel(job.id)
    for job in list(manager.jobs.values()):
        if job.thread is not None:
            job.thread.join(timeout=15)
    manager.jobs.clear()
    manager._active_lock_keys.clear()
    manager._queue.clear()
    manager._running.clear()
