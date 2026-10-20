"""The front end's Log out: what it calls, and that the job stream stops reconnecting (webui/src)."""

import re
from pathlib import Path

WEBUI = Path(__file__).parents[1] / "webui" / "src"


def test_log_out_ends_the_session_and_stops_the_job_stream() -> None:
    app = (WEBUI / "App.svelte").read_text(encoding="utf-8")
    sign_out = re.search(r"async function signOut\(\) \{(.*?)\n  \}", app, flags=re.DOTALL)

    assert '<button class="btn secondary small" onclick={signOut}>Log out</button>' in app
    assert sign_out is not None
    assert "await logout()" in sign_out[1]
    assert "jobStore.stop()" in sign_out[1]


def test_a_stopped_job_stream_does_not_reconnect() -> None:
    store = (WEBUI / "lib" / "jobs.svelte.ts").read_text(encoding="utf-8")
    on_close = re.search(r"ws\.onclose = \(\) => \{(.*?)\n    \}", store, flags=re.DOTALL)

    assert on_close is not None
    assert "if (this.started) this.retry = setTimeout(() => this.connect(), 2000)" in on_close[1]
    assert "this.started = false" in store
