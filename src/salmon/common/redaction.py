import ipaddress
import re
import shlex
import string
from collections.abc import Callable, Iterable
from urllib.parse import parse_qsl, quote, unquote, urlsplit

# Up to the authority's end: a "@" later in the query or fragment is no userinfo.
_URL_USERINFO = re.compile(r"(://)[^/?#\s@]+@")
_USERINFO_PASSWORD = re.compile(r"://[^/\s@:]*:([^/\s@']+)@")
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
        "--timeout",
        "--contimeout",
        "--retries",
        "--low-level-retries",
        "--log-level",
        "--buffer-size",
        "--multi-thread-streams",
    }
)
# Switches that take no value. Any other flag takes the next argument as its value, whatever that looks
# like ("-abc" can be a password), so an unlisted switch at worst hides the argument after it.
_SWITCHES = frozenset(
    {
        "-v",
        "-vv",
        "-q",
        "-P",
        "--progress",
        "--quiet",
        "--dry-run",
        "--checksum",
        "--size-only",
        "--update",
        "--inplace",
        "--ignore-existing",
        "--no-traverse",
        "--no-check-certificate",
        "--stats-one-line",
    }
)
# A flag is -x or --name; a value may itself start with dashes (a PEM key's -----BEGIN).
_FLAG = re.compile(r"--?[A-Za-z]")
# A header line such as rclone's --dump auth prints ("Authorization: Bearer ..."): the value is masked.
_SECRET_HEADER = re.compile(rf"(?im)^([^\S\n]*[\w-]*{_SECRET_WORDS}[\w-]*[^\S\n]*:)[^\n]*")
# A secret field of an rclone connection string (":sftp,pass=x,user=y:"); a bare value ends at , or :.
_CONNECTION_SECRET = re.compile(
    rf"""(?:^|[,:])[\w-]*{_SECRET_WORDS}[\w-]*=('(?:[^']|'')*'|"(?:[^"]|"")*"|[^,:\s]+)""", re.IGNORECASE
)


# The names a secret goes by, in a JSON field, a URL query, a cookie header or a form input: one list for all three.
_SECRET_NAMES = "authkey|passkey|torrent_pass|auth|api_?key|authorization|session|keeplogged|imgauth_[hue]"
# "authkey": "..." in JSON or a Python repr of it, authkey = "..." in a page's script. A value ends at its closing
# quote, or at its line's end when an excerpt cut it off; a backslash escapes the next character.
_SENSITIVE_KEYS = re.compile(
    rf"""(["']?\b(?:{_SECRET_NAMES}|Authorization)["']?\s*[:=]\s*)(["'])(?:(?!\2)[^\\\n]|\\.)*(\2?)""", re.IGNORECASE
)
# A value ends at "; ", a cookie's attribute separator; a ";" with no space after stays in it, as in a URL query.
_SENSITIVE_URL_PARAMS = re.compile(rf"\b({_SECRET_NAMES})=(?:[^&;\"'\s<>]|;(?!\s))+", re.IGNORECASE)
# The passkey in an announce URL, wherever the text holds one and however short, as in redact_url.
_ANNOUNCE_PASSKEY = re.compile(r"/[A-Za-z0-9]+(?=/announce)")

# A form field carrying one, <input name="auth" value="...">, its attributes in any order, quoted or not.
# Quote-aware: a ">" inside a quoted attribute does not end the tag.
_INPUT_TAG = re.compile(r"""<input\b(?:[^>"']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)
_SECRET_FIELD_NAME = re.compile(rf"""\bname\s*=\s*(["']?)(?:{_SECRET_NAMES})\1(?=[\s/>])""", re.IGNORECASE)
_FIELD_VALUE = re.compile(r"""(\bvalue\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s"'>]+)""", re.IGNORECASE)


def _mask_secret_field(match: re.Match[str]) -> str:
    tag = match[0]
    if not _SECRET_FIELD_NAME.search(tag):
        return tag
    return _FIELD_VALUE.sub(r'\1"[REDACTED]"', tag)


def _mask_known(text: str, secrets: Iterable[str | None], shortest: int) -> str:
    """Mask each secret of at least `shortest` characters, longest first so one inside another goes whole."""
    for secret in sorted(dict.fromkeys(s for s in secrets if s and len(s) >= shortest), key=len, reverse=True):
        # A short one only as a whole word, so "ab" doesn't eat "about".
        pattern = re.escape(secret) if len(secret) >= 3 else rf"(?<!\w){re.escape(secret)}(?!\w)"
        text = re.sub(pattern, "[REDACTED]", text)
    return text


def redact_secrets(text: str, known: Iterable[str | None] = ()) -> str:
    """Mask known secret values, URL userinfo and password/token flags before text reaches a log."""
    # Known values first: an error can repeat one in a shape no pattern expects.
    text = _mask_known(text, known, shortest=1)
    text = _SECRET_HEADER.sub(r"\1 [REDACTED]", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = _SECRET_FLAG.sub(r"\1[REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", text)


def redact_tracker_fields(text: str) -> str:
    """Redact sensitive values (quoted fields and assignments, URL query params and userinfo, form inputs, passkeys)."""
    text = _INPUT_TAG.sub(_mask_secret_field, text)
    text = _SENSITIVE_URL_PARAMS.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _ANNOUNCE_PASSKEY.sub("/[REDACTED]", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    # Last: a value with no closing quote runs to the next quote, which can open another pattern's match.
    return _SENSITIVE_KEYS.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(3)}", text)


# A response header naming a credential. A cookie keeps its name and attributes, only its value is masked.
_SECRET_HEADER_NAME = re.compile(r"auth|token|key|session|pass", re.IGNORECASE)
_COOKIE_VALUE = re.compile(r"^(\s*[^=;\s]+=)[^;]*")


def redact_tracker_headers(
    headers: Iterable[tuple[str, str]], scrub: Callable[[str], str] = redact_tracker_fields
) -> dict[str, str]:
    """A tracker's response headers for the log, with credentials and cookie values masked."""
    shown: dict[str, str] = {}
    for name, value in headers:
        if "cookie" in name.lower():
            value = _COOKIE_VALUE.sub(r"\1[REDACTED]", value)
        elif _SECRET_HEADER_NAME.search(name):
            value = "[REDACTED]"
        value = scrub(value)
        # A header sent more than once (Set-Cookie) is shown once, its values joined as HTTP folds them.
        shown[name] = f"{shown[name]}, {value}" if name in shown else value
    return shown


def safe_response_excerpt(text: str, limit: int = 500) -> str:
    """A redacted, length-capped excerpt of a tracker response for error messages."""
    redacted = redact_tracker_fields(text)
    return redacted if len(redacted) <= limit else redacted[:limit] + "… [truncated]"


def scrub_secrets(text: str, secrets: Iterable[str | None], shortest: int = 8) -> str:
    """redact_tracker_fields, plus every secret of at least `shortest` characters."""
    return _mask_known(redact_tracker_fields(text), secrets, shortest)


def _hidden_flag(arg: str) -> bool:
    """A flag whose value isn't on the shown list."""
    return _FLAG.match(arg) is not None and arg not in _SHOWN_FLAGS


def _takes_hidden_value(arg: str) -> bool:
    """A hidden flag given as "--flag value", whose next argument is its value."""
    return _hidden_flag(arg) and "=" not in arg and arg not in _SWITCHES


def secret_values(args: Iterable[str], connection: str = "") -> list[str]:
    """Values given to a command that aren't known harmless, plus connection-string secrets, to mask wherever echoed."""
    args = list(args)
    found = [value for flag, value in zip(args, args[1:], strict=False) if _takes_hidden_value(flag)]
    for arg in args:
        name, equals, value = arg.partition("=")
        if equals and _hidden_flag(name):
            found.append(value)
    for match in _CONNECTION_SECRET.finditer(connection):
        value = match.group(1)
        if value[:1] in "'\"" and len(value) > 1:
            value = value[1:-1].replace(value[0] * 2, value[0])
        found.append(value)
    # A URL anywhere (an argument, a connection-string url= field) can carry user:password@.
    found += [match.group(1) for text in [*args, connection] for match in _USERINFO_PASSWORD.finditer(text)]
    # rclone takes some values as comma-separated lists (--http-headers Name,Value), echoed one part at a time.
    found += [part.strip(" '\"") for value in found if "," in value for part in value.split(",")]
    return found


_ID = re.compile(r"\d{1,19}")
_WORD = re.compile(r"[A-Za-z_]{1,32}")
# A query value shows only under these names, and only in the shape each takes; every other value is masked.
_SHOWN_QUERY = {"id": _ID, "torrentid": _ID, "groupid": _ID, "page": _ID, "action": _WORD}
# A query name shows only as a plain short name with no digit: one holding "=", a digit or a token is masked.
_SHOWN_NAME = re.compile(r"[A-Za-z_][A-Za-z_.\[\]-]{0,31}")
# A run this long could be a passkey or a token, wherever it sits in a URL and however it is encoded.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_]{20,}")


# A path segment shows only as a plain one once decoded: digits alone (an id) or no digit at all (a word or slug).
# One mixing letters and digits, as every random passkey and token does, is masked whatever its length.
_PLAIN_SEGMENT = re.compile(r"\d{1,19}|\D{0,64}")


def _ip_literal(host: str) -> str:
    """An IPv6 host: shown when it is an address, masked when it holds a zone (fe80::1%eth0) or is none."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "[REDACTED]"
    return host if not getattr(address, "scope_id", None) else "[REDACTED]"


def _segment(text: str) -> str:
    """A host label or path segment to show: masked unless, decoded, it is a plain id or word."""
    decoded = unquote(text)
    return text if _PLAIN_SEGMENT.fullmatch(decoded) and not _TOKEN_RUN.search(decoded) else "[REDACTED]"


def _query_name(name: str) -> str:
    return name if _SHOWN_NAME.fullmatch(name) and not _TOKEN_RUN.search(name) else "[REDACTED]"


def redact_url(url: str) -> str:
    """An http(s) URL to show: no userinfo, and its fragment and any token-like host, path or query part masked."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return "[REDACTED]"
    shown_host = _ip_literal(host) if ":" in host else ".".join(_segment(label) for label in host.split("."))
    netloc = (f"[{shown_host}]" if ":" in shown_host else shown_host) + (f":{port}" if port else "")
    segments = parts.path.split("/")
    # An announce URL's passkey sits next to "announce" (/<passkey>/announce), however short it is.
    beside_announce = {
        index + step
        for index, segment in enumerate(segments)
        if unquote(segment).lower().startswith("announce")
        for step in (-1, 1)
    }
    path = "/".join(
        "[REDACTED]" if index in beside_announce and segment else _segment(segment)
        for index, segment in enumerate(segments)
    )
    query = "&".join(
        f"{_query_name(name)}="
        + (value if (shape := _SHOWN_QUERY.get(name.lower())) and shape.fullmatch(value) else "[REDACTED]")
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    return (
        f"{parts.scheme}://{netloc}{path}" + (f"?{query}" if query else "") + ("#[REDACTED]" if parts.fragment else "")
    )


_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
# What requests leaves unescaped when it re-quotes a URL before sending it; with a bad escape anywhere in the
# URL ("%ZZ"), it escapes every "%" instead.
_URI_SAFE = "!#$%&'()*+,/:;=?@[]~"
_URI_SAFE_NO_PERCENT = _URI_SAFE.replace("%", "")
# Path segments a client's own endpoint is made of; any other segment could be a proxy's key, whatever its shape.
_CLIENT_PATH_WORDS = frozenset(
    {"api", "v2", "json", "proxy", "plugins", "rpc", "rpc.php", "rpc2", "httprpc", "action.php", "rutorrent"}
) | {"deluge", "transmission", "qbittorrent"}


def _requoted(text: str) -> str:
    """As requests re-quotes a URL and urllib3 repeats it: unreserved escapes decoded, the rest uppercased."""

    def escape(match: re.Match[str]) -> str:
        char = chr(int(match[0][1:], 16))
        return char if char in _UNRESERVED else match[0].upper()

    return quote(_ESCAPE.sub(escape, text), safe=_URI_SAFE)


def _echoed_forms(secret: str) -> list[str]:
    """A URL part as written, decoded, and re-escaped the ways an HTTP client does before an error repeats it."""
    upper = _ESCAPE.sub(lambda match: match[0].upper(), secret)
    return [secret, unquote(secret), upper, _requoted(secret), quote(secret, safe=_URI_SAFE_NO_PERCENT)]


def redact_client_url(url: str) -> str:
    """A torrent client URL to show: its scheme, host and port, with any userinfo, path, query and fragment masked."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[REDACTED]"
    _, at, host = parts.netloc.rpartition("@")
    return (
        f"{parts.scheme}://{'[REDACTED]@' if at else ''}{host}"
        + ("/[REDACTED]" if parts.path.strip("/") else "")
        + ("?[REDACTED]" if parts.query else "")
        + ("#[REDACTED]" if parts.fragment else "")
    )


def url_secrets(url: str) -> list[str]:
    """A URL's password, path, path segments but endpoint words, query and fragment, in the forms an error repeats."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return [url]
    path = parts.path.rstrip("/")
    keys = (segment for segment in path.split("/") if unquote(segment).casefold() not in _CLIENT_PATH_WORDS)
    found = [parts.password or "", path, *keys, parts.query, parts.fragment]
    return list(dict.fromkeys(form for secret in found if secret for form in _echoed_forms(secret)))


def redact_command(args: list[str], known: Iterable[str | None] = ()) -> str:
    """A command line for the log: only allowlisted flags keep their values, replaced before joining."""
    known = list(known)
    shown: list[str] = []
    hide_next = False
    for arg in args:
        name, equals, _ = arg.partition("=")
        if hide_next:
            shown.append("[REDACTED]")
            hide_next = False
            continue
        if equals and _hidden_flag(name):
            shown.append(f"{name}=[REDACTED]")
        else:
            shown.append(redact_secrets(arg, known))
        hide_next = _takes_hidden_value(arg)
    return shlex.join(shown)
