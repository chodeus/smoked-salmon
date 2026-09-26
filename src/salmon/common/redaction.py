import re
import shlex
from collections.abc import Iterable

_URL_USERINFO = re.compile(r"(://)[^/\s@]+@")
_SECRET_WORDS = r"(?:pass|password|token|secret|key|session)"
# A quoted value is masked whole, doubled quotes included: rclone quotes values with spaces (a PEM key).
_SECRET_VALUE = r"""(?:'(?:[^']|'')*'|"(?:[^"]|"")*"|\S+)"""
_SECRET_FLAG_NAME = re.compile(rf"--?[\w-]*{_SECRET_WORDS}[\w-]*", re.IGNORECASE)
_SECRET_FLAG = re.compile(rf"(--?[\w-]*{_SECRET_WORDS}[\w-]*(?:=|[ \t]+)){_SECRET_VALUE}", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(rf"\b([\w-]*{_SECRET_WORDS}[\w-]*)={_SECRET_VALUE}", re.IGNORECASE)


def redact_secrets(text: str, known: Iterable[str | None] = ()) -> str:
    """Mask known secret values, URL userinfo and password/token flags before text reaches a log."""
    # Known values first: an error can repeat one in a shape no pattern expects.
    for secret in sorted((s for s in known if s and len(s) >= 3), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = _SECRET_FLAG.sub(r"\1[REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", text)


def redact_command(args: list[str]) -> str:
    """A command line for the log, with each secret flag's value replaced before the arguments are joined."""
    shown: list[str] = []
    hide_next = False
    for arg in args:
        if hide_next:
            shown.append("[REDACTED]")
            hide_next = False
            continue
        hide_next = _SECRET_FLAG_NAME.fullmatch(arg) is not None
        shown.append(redact_secrets(arg))
    return shlex.join(shown)
