import re
import shlex
from collections.abc import Iterable

_URL_USERINFO = re.compile(r"(://)[^/\s@]+@")
_SECRET_WORDS = r"(?:pass|password|token|secret|key|session|header|cookie|auth)"
# A quoted value is masked whole, doubled quotes included: rclone quotes values with spaces (a PEM key).
_SECRET_VALUE = r"""(?:'(?:[^']|'')*'|"(?:[^"]|"")*"|\S+)"""
_SECRET_FLAG_NAME = re.compile(rf"--?[\w-]*{_SECRET_WORDS}[\w-]*", re.IGNORECASE)
_SECRET_FLAG = re.compile(rf"(--?[\w-]*{_SECRET_WORDS}[\w-]*(?:=|[ \t]+)){_SECRET_VALUE}", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(rf"\b([\w-]*{_SECRET_WORDS}[\w-]*)={_SECRET_VALUE}", re.IGNORECASE)
# Flags whose values are harmless to show; every other flag's value in a command is masked, since no
# list of secret flag names ever covers them all (rclone takes credentials in headers, keys, tokens ...).
_SHOWN_FLAGS = frozenset(
    {
        "--transfers",
        "--checkers",
        "--bwlimit",
        "--config",
        "--sftp-path-override",
        "--timeout",
        "--contimeout",
        "--retries",
        "--low-level-retries",
        "--log-level",
        "--buffer-size",
        "--multi-thread-streams",
    }
)
# A secret field of an rclone connection string (":sftp,pass=x,user=y:"); a bare value ends at , or :.
_CONNECTION_SECRET = re.compile(
    rf"""(?:^|[,:])[\w-]*{_SECRET_WORDS}[\w-]*=('(?:[^']|'')*'|"(?:[^"]|"")*"|[^,:\s]+)""", re.IGNORECASE
)


def redact_secrets(text: str, known: Iterable[str | None] = ()) -> str:
    """Mask known secret values, URL userinfo and password/token flags before text reaches a log."""
    # Known values first: an error can repeat one in a shape no pattern expects.
    for secret in sorted((s for s in known if s), key=len, reverse=True):
        # A short one only as a whole word, so "ab" doesn't eat "about".
        pattern = re.escape(secret) if len(secret) >= 3 else rf"(?<!\w){re.escape(secret)}(?!\w)"
        text = re.sub(pattern, "[REDACTED]", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = _SECRET_FLAG.sub(r"\1[REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", text)


def _hidden_flag(arg: str) -> bool:
    """A flag whose value isn't on the shown list."""
    return arg.startswith("-") and arg not in _SHOWN_FLAGS


def secret_values(args: Iterable[str], connection: str = "") -> list[str]:
    """Values given to a command that aren't known harmless, plus connection-string secrets, to mask wherever echoed."""
    args = list(args)
    found = [
        value
        for flag, value in zip(args, args[1:], strict=False)
        if _hidden_flag(flag) and "=" not in flag and not value.startswith("-")
    ]
    for arg in args:
        name, equals, value = arg.partition("=")
        if equals and _hidden_flag(name):
            found.append(value)
    # rclone takes some values as comma-separated lists (--http-headers Name,Value), echoed one part at a time.
    found += [part.strip(" '\"") for value in found if "," in value for part in value.split(",")]
    for match in _CONNECTION_SECRET.finditer(connection):
        value = match.group(1)
        if value[:1] in "'\"" and len(value) > 1:
            value = value[1:-1].replace(value[0] * 2, value[0])
        found.append(value)
    return found


def redact_command(args: list[str], known: Iterable[str | None] = ()) -> str:
    """A command line for the log: only allowlisted flags keep their values, replaced before joining."""
    known = list(known)
    shown: list[str] = []
    hide_next = False
    for arg in args:
        name, equals, _ = arg.partition("=")
        if hide_next and not arg.startswith("-"):
            shown.append("[REDACTED]")
            hide_next = False
            continue
        if equals and _hidden_flag(name):
            shown.append(f"{name}=[REDACTED]")
        else:
            shown.append(redact_secrets(arg, known))
        hide_next = _hidden_flag(arg) and not equals
    return shlex.join(shown)
