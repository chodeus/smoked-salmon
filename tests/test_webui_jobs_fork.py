"""What this fork's job manager adds to upstream's: a failed job's traceback reaches the server's stderr masked, and
a job on an album's disc folder waits for one on the album."""

from collections.abc import Callable
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient
from test_webui_jobs import (  # noqa: F401  # pyright: ignore[reportMissingImports]
    SECRETS,
    Params,
    Run,
    _finished,
    _gate,
    _start,
    _until,
    _with_app,
    kind,
    planted,
)

from salmon.webui.jobs import JobManager


def test_a_failed_jobs_traceback_reaches_the_servers_stderr_masked(
    planted: None,  # noqa: F811
    kind: Callable[[str, Run], None],  # noqa: F811
    capfd: pytest.CaptureFixture[str],
) -> None:
    async def run(_params: Params) -> None:
        raise RuntimeError(f"GET https://red.example/ajax.php?key={SECRETS['api_key']} failed")

    kind("fails", run)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "fails"))
        assert job.status == "failed"

    _with_app(test)
    err = capfd.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: GET https://red.example/ajax.php?key=" in err
    assert SECRETS["api_key"] not in err


def test_a_job_that_fails_before_its_run_has_its_traceback_masked_too(
    planted: None,  # noqa: F811
    kind: Callable[[str, Run], None],  # noqa: F811
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(_params: Params) -> None:
        return None

    async def broken(_manager: JobManager, _job: object) -> None:
        raise RuntimeError(f"the run could not start with {SECRETS['session']}")

    kind("starts", run)
    monkeypatch.setattr(JobManager, "_run", broken)

    async def test(client: TestClient, manager: JobManager) -> None:
        job = await _finished(manager, await _start(client, "starts"))
        assert job.status == "failed"
        assert SECRETS["session"] not in (job.error or "")

    _with_app(test)
    err = capfd.readouterr().err
    assert "RuntimeError: the run could not start with" in err
    assert SECRETS["session"] not in err


@pytest.mark.parametrize(
    ("busy", "waiting"), [("Album", "Album/CD1"), ("Album/CD1", "Album")], ids=["inside", "holding"]
)
def test_a_job_on_a_folder_inside_or_holding_a_busy_one_waits(
    kind: Callable[[str, Run], None],  # noqa: F811
    tmp_path: Path,
    busy: str,
    waiting: str,
) -> None:
    gate, run = _gate()
    kind("gated", run)
    (tmp_path / "Album" / "CD1").mkdir(parents=True)
    # Its name starts with the album's, yet it is another folder.
    (tmp_path / "Album 2").mkdir()

    async def test(client: TestClient, manager: JobManager) -> None:
        first = await _start(client, "gated", folder=str(tmp_path / busy))
        nested = await _start(client, "gated", folder=str(tmp_path / waiting))
        sibling = await _start(client, "gated", folder=str(tmp_path / "Album 2"))
        await _until(lambda: manager.jobs[sibling].status == "running")
        assert manager.jobs[first].status == "running"
        assert manager.jobs[nested].status == "queued"
        gate.set()
        for each in (first, nested, sibling):
            assert (await _finished(manager, each)).status == "done"

    _with_app(test, max_jobs=3)
