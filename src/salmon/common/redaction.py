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
# A secret field of an rclone connection string (":sftp,pass=x,user=y:"); a bare value ends at , or :.
_CONNECTION_SECRET = re.compile(
    rf"""(?:^|[,:])[\w-]*{_SECRET_WORDS}[\w-]*=('(?:[^']|'')*'|"(?:[^"]|"")*"|[^,:\s]+)""", re.IGNORECASE
)


def redact_secrets(text: str, known: Iterable[str | None] = ()) -> str:
    """Mask known secret values, URL userinfo and password/token flags before text reaches a log."""
    # Known values first: an error can repeat one in a shape no pattern expects.
    for secret in sorted((s for s in known if s and len(s) >= 3), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = _SECRET_FLAG.sub(r"\1[REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", text)


def secret_values(args: Iterable[str], connection: str = "") -> list[str]:
    """The secret values given to a command (flag values and connection-string fields), to mask wherever echoed."""
    args = list(args)
    found = [value for flag, value in zip(args, args[1:], strict=False) if _SECRET_FLAG_NAME.fullmatch(flag)]
    for arg in args:
        name, equals, value = arg.partition("=")
        if equals and _SECRET_FLAG_NAME.fullmatch(name):
            found.append(value)
    for match in _CONNECTION_SECRET.finditer(connection):
        value = match.group(1)
        if value[:1] in "'\"" and len(value) > 1:
            value = value[1:-1].replace(value[0] * 2, value[0])
        found.append(value)
    return found


def redact_command(args: list[str], known: Iterable[str | None] = ()) -> str:
    """A command line for the log, with each secret flag's value replaced before the arguments are joined."""
    known = list(known)
    shown: list[str] = []
    hide_next = False
    for arg in args:
        name, equals, _ = arg.partition("=")
        if hide_next:
            shown.append("[REDACTED]")
        elif equals and _SECRET_FLAG_NAME.fullmatch(name):
            shown.append(f"{name}=[REDACTED]")
        else:
            shown.append(redact_secrets(arg, known))
        hide_next = not hide_next and _SECRET_FLAG_NAME.fullmatch(arg) is not None
    return shlex.join(shown)
