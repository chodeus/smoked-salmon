"""Context-local progress reporting for long-running operations.

The web interface sets a callback for the duration of a job; `process_files`
and friends report per-file progress through it. In plain CLI usage no
callback is set and reporting is a no-op (tqdm remains the CLI progress UI).
"""

import contextlib
import contextvars
from collections.abc import Callable
from contextvars import Token

ProgressCallback = Callable[[int, int, str], None]
"""Callback signature: (files_done, files_total, description)."""

_progress_callback: contextvars.ContextVar[ProgressCallback | None] = contextvars.ContextVar(
    "salmon_progress_callback", default=None
)


def set_progress_callback(callback: ProgressCallback | None) -> Token:
    """Set the progress callback for the current context. Returns a reset token."""
    return _progress_callback.set(callback)


def reset_progress_callback(token: Token) -> None:
    """Restore the previous progress callback."""
    _progress_callback.reset(token)


def report_progress(done: int, total: int, desc: str) -> None:
    """Report progress to the context's callback, if any. Never raises."""
    callback = _progress_callback.get()
    if callback is None:
        return
    with contextlib.suppress(Exception):
        callback(done, total, desc)
