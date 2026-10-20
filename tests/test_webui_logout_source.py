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
    # A failed logout (not a 401) leaves the session alive, so the page must not show the login screen.
    assert "signOutError = `Log out failed: ${e}`\n        return" in sign_out[1]


def _body(source: str, head: str) -> str:
    """The body of the method starting with ``head``, up to its closing brace at that indent."""
    found = re.search(re.escape(head) + r"(.*?)\n  \}", source, flags=re.DOTALL)
    assert found is not None, head
    return found[1]


def test_a_stopped_job_stream_does_not_reconnect() -> None:
    store = (WEBUI / "lib" / "jobs.svelte.ts").read_text(encoding="utf-8")
    on_close = re.search(r"ws\.onclose = \(\) => \{(.*?)\n    \}", store, flags=re.DOTALL)

    assert on_close is not None
    assert "this.scheduleReconnect()" in on_close[1]
    assert _body(store, "private scheduleReconnect() {").lstrip().startswith("if (!this.started) return")
    assert "this.started = false" in store
    # A stopped socket's close event, coming after a new login, must not reconnect a second time.
    assert "this.ws.onclose = null" in store


def test_the_job_stream_checks_the_login_before_reconnecting() -> None:
    # A refused websocket says nothing of why: after a restart it would retry forever on a lost login.
    store = (WEBUI / "lib" / "jobs.svelte.ts").read_text(encoding="utf-8")
    api = (WEBUI / "lib" / "api.ts").read_text(encoding="utf-8")
    app = (WEBUI / "App.svelte").read_text(encoding="utf-8")
    connect = _body(store, "private async connect() {")
    confirm = _body(api, "export async function confirmLoggedIn(): Promise<void> {")

    assert connect.index("await confirmLoggedIn()") < connect.index("new WebSocket(wsUrl())")
    assert "if (!this.started || stops !== this.stops) return\n    const ws = new WebSocket" in connect
    assert "if (stops === this.stops) this.scheduleReconnect()" in connect
    assert "if (state.required && !state.authenticated) {\n    unauthorizedHandler?.()\n    throw" in confirm
    assert "if (authed) jobStore.init()\n    else jobStore.stop()" in app


def test_a_job_list_loaded_across_a_logout_is_dropped() -> None:
    store = (WEBUI / "lib" / "jobs.svelte.ts").read_text(encoding="utf-8")
    resync = _body(store, "private async resync() {")

    assert "this.stops++" in _body(store, "stop() {")
    assert resync.lstrip().startswith("if (!this.started) return")
    assert resync.index("const stops = this.stops") < resync.index("await apiGet<Job[]>('/jobs')")
    assert resync.count("if (stops !== this.stops) return") == 2
    assert "await apiGet<Job[]>('/jobs')\n      if (stops !== this.stops) return" in resync
    assert "} catch (e) {\n      if (stops !== this.stops) return" in resync
