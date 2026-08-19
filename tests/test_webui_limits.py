"""Concurrency caps for the job manager (H3)."""

import asyncio

import pytest

from salmon.webui import jobs as jobs_mod
from salmon.webui.jobs import JobCapacityError, JobManager


async def _block(_job):
    await asyncio.Event().wait()  # runs until the job is cancelled


async def test_threaded_job_cap_raises_capacity_error(monkeypatch):
    # async so _register can capture a running server loop, like a real route
    monkeypatch.setattr(jobs_mod, "MAX_ACTIVE_THREAD_JOBS", 2)
    m = JobManager()
    j1 = m.create_threaded("t", "1", _block, lock_key="a")
    j2 = m.create_threaded("t", "2", _block, lock_key="b")
    try:
        with pytest.raises(JobCapacityError):
            m.create_threaded("t", "3", _block, lock_key="c")
    finally:
        m.cancel(j1.id)
        m.cancel(j2.id)


def test_subscriber_cap_returns_none(monkeypatch):
    monkeypatch.setattr(jobs_mod, "MAX_SUBSCRIBERS", 1)
    m = JobManager()
    first = m.subscribe()
    assert first is not None
    assert m.subscribe() is None  # over the cap
    m.unsubscribe(first)
    assert m.subscribe() is not None  # freed up again
