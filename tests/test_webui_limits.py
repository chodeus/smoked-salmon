"""Concurrency caps for the job manager (H3); the job queue's are in test_webui_jobs_api."""

from salmon.webui import jobs as jobs_mod
from salmon.webui.jobs import JobManager


def test_subscriber_cap_returns_none(monkeypatch):
    monkeypatch.setattr(jobs_mod, "MAX_SUBSCRIBERS", 1)
    m = JobManager()
    first = m.subscribe()
    assert first is not None
    over_cap = m.subscribe()
    assert over_cap is None  # over the cap
    m.unsubscribe(first)
    freed = m.subscribe()
    assert freed is not None  # freed up again
