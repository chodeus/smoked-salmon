"""The browser's job statuses are the server's: a status it doesn't know would leave a job without its chip."""

import pathlib
import re

from salmon.webui.jobs import FINISHED

JOBS_STORE = pathlib.Path(__file__).resolve().parent.parent / "webui" / "src" / "lib" / "jobs.svelte.ts"


def test_the_browsers_job_statuses_are_the_servers():
    found = re.search(r"^export type JobStatus = (.+)$", JOBS_STORE.read_text(encoding="utf-8"), re.MULTILINE)
    assert found is not None
    statuses = set(re.findall(r"'(\w+)'", found.group(1)))
    assert statuses == {"queued", "running"} | FINISHED
