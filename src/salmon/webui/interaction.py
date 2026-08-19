"""Bridge between salmon's terminal prompts and browser questions.

Web upload jobs run the unmodified upload pipeline in a worker thread with its
own event loop. Module-level patches on asyncclick (and a few salmon-internal
seams) dispatch on a context variable: inside a web job every prompt becomes a
question event that the browser answers via the jobs API; outside (CLI, plain
webui jobs) the original terminal behavior is untouched.

asyncclick in the pinned version has an async ``prompt`` but synchronous
``confirm``/``edit`` — the worker-thread architecture exists precisely so the
sync ones may block their own loop while the server loop delivers the answer.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import itertools
import os
import threading
from typing import TYPE_CHECKING, Any

import asyncclick

import salmon.commands
import salmon.tagger.pre_data
import salmon.uploader
import salmon.uploader.spectrals

if TYPE_CHECKING:
    from collections.abc import Callable

_question_counter = itertools.count(1)

_current: contextvars.ContextVar[WebInteraction | None] = contextvars.ContextVar("web_interaction", default=None)


def current_interaction() -> WebInteraction | None:
    return _current.get()


def set_interaction(interaction: WebInteraction | None) -> contextvars.Token:
    return _current.set(interaction)


class WebInteraction:
    """Routes prompts from a job thread to the browser and waits for answers."""

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        set_question: Callable[[dict[str, Any] | None], None],
    ):
        self._emit = emit
        self._set_question = set_question
        self._lock = threading.Lock()
        self._pending: tuple[str, concurrent.futures.Future] | None = None
        self.spectrals: dict[str, Any] | None = None
        self.cancel_requested = threading.Event()
        self.closed = threading.Event()
        self._line_buffer = ""

    def emit(self, event: dict[str, Any]) -> None:
        if not self.closed.is_set():
            self._emit(event)

    def log(self, line: str) -> None:
        self.emit({"event": "log", "line": line})

    def log_fragment(self, text: str, nl: bool) -> None:
        """Assemble nl=False echo fragments into whole lines (listing rows)."""
        if nl:
            self.log(self._line_buffer + text)
            self._line_buffer = ""
        else:
            self._line_buffer += text

    def flush_log(self) -> None:
        if self._line_buffer:
            self.log(self._line_buffer)
            self._line_buffer = ""

    def _begin(
        self,
        kind: str,
        text: str,
        *,
        default: Any = None,
        choices: list[str] | None = None,
        initial: str | None = None,
    ) -> tuple[str, concurrent.futures.Future]:
        if self.cancel_requested.is_set() or self.closed.is_set():
            raise concurrent.futures.CancelledError()
        self.flush_log()
        question = {
            "id": f"q-{next(_question_counter)}",
            "kind": kind,
            "text": asyncclick.unstyle(text or "").strip(),
            "default": default,
            "choices": choices,
            "initial": initial,
        }
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._pending = (question["id"], future)
        self._set_question(question)
        self.emit({"event": "question", "question": question})
        return question["id"], future

    def _finish(self, question_id: str) -> None:
        with self._lock:
            self._pending = None
        self._set_question(None)
        self.emit({"event": "question_answered", "question_id": question_id})

    def ask_sync(self, kind: str, text: str, **kw: Any) -> Any:
        """Ask from synchronous code; blocks the job thread until answered."""
        question_id, future = self._begin(kind, text, **kw)
        try:
            return future.result()
        finally:
            self._finish(question_id)

    async def ask_async(self, kind: str, text: str, **kw: Any) -> Any:
        """Ask from the job's event loop without blocking it."""
        question_id, future = self._begin(kind, text, **kw)
        try:
            return await asyncio.wrap_future(future)
        finally:
            self._finish(question_id)

    def answer(self, question_id: str, value: Any) -> bool:
        """Deliver an answer (called from the server loop). False if stale."""
        with self._lock:
            if self._pending is None or self._pending[0] != question_id:
                return False
            future = self._pending[1]
        try:
            future.set_result(value)
        except concurrent.futures.InvalidStateError:
            return False
        return True

    def cancel_pending(self) -> None:
        """Request cancellation: fail the pending question and reject future ones."""
        self.cancel_requested.set()
        with self._lock:
            if self._pending is not None:
                self._pending[1].cancel()

    def close(self) -> None:
        """Stop emitting events and asking questions (job is finished)."""
        self.closed.set()
        with self._lock:
            if self._pending is not None:
                self._pending[1].cancel()


# ---------------------------------------------------------------------------
# Patches
# ---------------------------------------------------------------------------

_originals: dict[str, Any] = {}


def _coerce_default(raw: Any, default: Any) -> Any | None:
    """None or empty string means: take the prompt's default (if any)."""
    if (raw is None or raw == "") and default is not None:
        return default
    return raw


async def _web_prompt(text: str, default: Any = None, type: Any = None, value_proc: Any = None, **kw: Any) -> Any:
    interaction = _current.get()
    if interaction is None:
        return await _originals["prompt"](text, default=default, type=type, value_proc=value_proc, **kw)
    choices = list(type.choices) if isinstance(type, asyncclick.Choice) else None
    while True:
        raw = await interaction.ask_async("prompt", text, default=default, choices=choices)
        value = _coerce_default(raw, default)
        # click re-prompts on empty input unless a default exists.
        if value is None or (value == "" and default is None):
            interaction.log("An answer is required.")
            continue
        if not isinstance(value, str):
            value = str(value)
        try:
            if value_proc is not None:
                return value_proc(value)
            if type is not None and isinstance(type, asyncclick.ParamType):
                return type.convert(value, None, None)
        except (asyncclick.UsageError, ValueError, TypeError) as e:
            interaction.log(f"Invalid answer: {e}")
            continue
        return value


def _web_confirm(text: str, default: bool | None = False, abort: bool = False, **kw: Any) -> bool:
    interaction = _current.get()
    if interaction is None:
        return _originals["confirm"](text, default=default, abort=abort, **kw)
    while True:
        raw = interaction.ask_sync("confirm", text, default=default)
        if isinstance(raw, bool):
            value = raw
            break
        answer = "" if raw is None else str(raw).strip().lower()
        if answer == "" and default is not None:
            value = default
            break
        if answer in ("y", "yes", "true", "1"):
            value = True
            break
        if answer in ("n", "no", "false", "0"):
            value = False
            break
        interaction.log("Please answer yes or no.")
    if abort and not value:
        raise asyncclick.Abort()
    return value


def _web_edit(text: str | None = None, **kw: Any) -> str | None:
    interaction = _current.get()
    if interaction is None or kw.get("filename") is not None:
        return _originals["edit"](text, **kw)
    prompt_text = "Edit the text below and save, or discard to keep it unchanged."
    raw = interaction.ask_sync("edit", prompt_text, initial=text or "")
    if raw is None:
        return None
    raw = str(raw)
    # click.edit guarantees a trailing newline on save; callers strip it.
    return raw if raw.endswith("\n") else raw + "\n"


def _web_input(text: str = "") -> str:
    interaction = _current.get()
    if interaction is None:
        return input(text)
    raw = interaction.ask_sync("prompt", str(text), default=None)
    return "" if raw is None else str(raw)


def _web_echo(message: Any = None, **kw: Any) -> None:
    interaction = _current.get()
    if interaction is not None and not kw.get("err"):
        text = "" if message is None else asyncclick.unstyle(str(message))
        interaction.log_fragment(text, kw.get("nl", True))
    _originals["echo"](message, **kw)


def _web_secho(message: Any = None, **kw: Any) -> None:
    interaction = _current.get()
    if interaction is not None:
        text = "" if message is None else asyncclick.unstyle(str(message))
        interaction.log_fragment(text, kw.get("nl", True))
    _originals["secho"](message, **kw)


async def _web_view_spectrals(spectrals_path: str, all_spectral_ids: dict[int, str]) -> None:
    interaction = _current.get()
    if interaction is None:
        await _originals["view_spectrals"](spectrals_path, all_spectral_ids)
        return
    files = sorted(f for f in os.listdir(spectrals_path) if f.lower().endswith(".png"))
    interaction.spectrals = {"path": spectrals_path, "files": files}
    interaction.emit({"event": "spectrals_ready", "files": files})
    interaction.log("Spectrals are shown in the browser below.")


async def _web_handle_spectrals_upload_and_deletion(*args: Any, **kw: Any) -> Any:
    result = await _originals["handle_spectrals_upload_and_deletion"](*args, **kw)
    interaction = _current.get()
    if interaction is not None:
        # The spectrals folder is gone now; stop advertising its images.
        interaction.spectrals = None
        interaction.emit({"event": "spectrals_ready", "files": []})
    return result


def _web_flush_stdin() -> None:
    if _current.get() is None:
        _originals["flush_stdin"]()


def install_interaction_patches() -> None:
    """Install the context-dispatching patches. Idempotent; call at server start."""
    if _originals:
        return
    _originals.update(
        prompt=asyncclick.prompt,
        confirm=asyncclick.confirm,
        edit=asyncclick.edit,
        echo=asyncclick.echo,
        secho=asyncclick.secho,
        view_spectrals=salmon.uploader.spectrals.view_spectrals,
        flush_stdin=salmon.uploader.spectrals.flush_stdin,
        handle_spectrals_upload_and_deletion=salmon.uploader.spectrals.handle_spectrals_upload_and_deletion,
    )
    asyncclick.prompt = _web_prompt
    asyncclick.confirm = _web_confirm
    asyncclick.edit = _web_edit
    asyncclick.echo = _web_echo
    asyncclick.secho = _web_secho
    salmon.uploader.spectrals.view_spectrals = _web_view_spectrals
    salmon.uploader.spectrals.flush_stdin = _web_flush_stdin
    # handle_spectrals_upload_and_deletion is imported by name into other
    # modules; patch every namespace that holds a reference.
    salmon.uploader.spectrals.handle_spectrals_upload_and_deletion = _web_handle_spectrals_upload_and_deletion
    salmon.uploader.handle_spectrals_upload_and_deletion = _web_handle_spectrals_upload_and_deletion
    salmon.commands.handle_spectrals_upload_and_deletion = _web_handle_spectrals_upload_and_deletion
    # pre_data._prompt_encoding uses the raw builtin input(); a module-level
    # name shadows the builtin lookup.
    setattr(salmon.tagger.pre_data, "input", _web_input)  # noqa: B010 - shadows the builtin, not a real attribute
