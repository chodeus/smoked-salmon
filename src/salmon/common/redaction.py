import re

_URL_USERINFO = re.compile(r"(://)[^/\s@]+@")
_SECRET_WORDS = r"(?:pass|password|token|secret|key|session)"
# A quoted value is masked whole: rclone connection strings may quote one with spaces (a PEM key).
_SECRET_VALUE = r"""(?:'[^']*'|"[^"]*"|\S+)"""
_SECRET_FLAG = re.compile(rf"(--?[\w-]*{_SECRET_WORDS}[\w-]*(?:=|[ \t]+)){_SECRET_VALUE}", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(rf"\b([\w-]*{_SECRET_WORDS}[\w-]*)={_SECRET_VALUE}", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Mask URL userinfo and password/token flags before a command line or error message reaches a log."""
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = _SECRET_FLAG.sub(r"\1[REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", text)
