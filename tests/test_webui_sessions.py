"""Upstream's login sessions (auth.py ``Auth``): capped in number, and gone once they expire."""

import pytest

from salmon.webui import auth

TOKEN = "correct-horse-battery-staple-0123456789"


def test_sessions_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "MAX_SESSIONS", 3)
    gate = auth.Auth(TOKEN)
    first = gate.open_session()
    later = [gate.open_session() for _ in range(3)]
    assert not gate.session_valid(first)
    assert all(gate.session_valid(session) for session in later)


def test_sessions_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    gate = auth.Auth(TOKEN)
    session = gate.open_session()
    now[0] += auth.SESSION_MAX_AGE - 1
    assert gate.session_valid(session)
    now[0] += 2
    assert not gate.session_valid(session)
