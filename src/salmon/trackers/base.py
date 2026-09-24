import asyncio
import html
import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from http import HTTPStatus
from typing import Any, NoReturn, cast
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from weakref import WeakKeyDictionary

import aiohttp
import asyncclick as click
import msgspec
from aiohttp import FormData
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential, wait_random
from torf import TorfError, Torrent

from salmon import cfg
from salmon.common import UploadFiles
from salmon.constants import RELEASE_TYPES
from salmon.errors import (
    LoginError,
    RequestError,
    RequestFailedError,
    UnknownOutcomeError,
    UploadError,
)

ARTIST_TYPES = [
    "main",
    "guest",
    "remixer",
    "composer",
    "conductor",
    "djcompiler",
    "producer",
    "arranger",
]

INVERTED_RELEASE_TYPES = {
    **dict(zip(RELEASE_TYPES.values(), RELEASE_TYPES.keys(), strict=False)),
    1024: "Guest Appearance",
    1023: "Remixed By",
    1022: "Composition",
    1021: "Produced By",
}

_SENSITIVE_KEYS = re.compile(
    r'"(authkey|passkey|auth|api_key|Authorization|session|keeplogged)"\s*:\s*"[^"]*"',
    re.IGNORECASE,
)
# Same secrets as they appear in HTML/URLs/cookie headers (download links carry
# authkey/torrent_pass; Cookie/Set-Cookie carry session/keeplogged).
_SENSITIVE_URL_PARAMS = re.compile(
    r"\b(authkey|passkey|torrent_pass|auth|api_key|session|keeplogged)=[^&\"'\s<>]+",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """Redact sensitive values (JSON fields and URL query params) from a string."""
    text = _SENSITIVE_KEYS.sub(lambda m: f'"{m.group(1)}": "[REDACTED]"', text)
    return _SENSITIVE_URL_PARAMS.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)


def _safe_response_excerpt(text: str, limit: int = 500) -> str:
    """A redacted, length-capped excerpt of a tracker response for error messages.

    Upload responses are whole HTML pages whose download links embed authkey/
    torrent_pass; surfacing them raw leaks the passkey into job state.
    """
    redacted = _redact(text)
    return redacted if len(redacted) <= limit else redacted[:limit] + "… [truncated]"


def _normalize_session_cookie(cookie: str) -> str:
    """Normalize session cookies so aiohttp sends them in a browser-like form.

    RED-style session cookies often contain characters like `/`, `+`, `:`, and `=`.
    If we pass the decoded value directly into aiohttp's cookie jar, it gets wrapped
    in quotes when serialized into the Cookie header. Normalizing to a canonical
    percent-encoded form keeps the header unquoted and matches what browsers send.

    Args:
        cookie: Raw or already-encoded session cookie value from config.

    Returns:
        Canonical percent-encoded cookie value.
    """
    return quote(unquote(cookie.strip()), safe="")


def _build_tracker_cookies(session_cookie: str, keeplogged_cookie: str | None = None) -> dict[str, str]:
    """Build the cookie payload for tracker requests.

    Args:
        session_cookie: The tracker session cookie value.
        keeplogged_cookie: Optional persistent-login cookie value.

    Returns:
        Cookie mapping ready for aiohttp.
    """
    cookies = {"session": _normalize_session_cookie(session_cookie)}
    if keeplogged_cookie:
        cookies["keeplogged"] = keeplogged_cookie.strip()
    return cookies


def _add_form_field(form: FormData, key: str, value: Any) -> None:
    """Add a single value to FormData, coercing types as needed.

    aiohttp FormData only accepts str/bytes/IO types, so bool and int
    values are converted accordingly. False and None are skipped.

    Args:
        form: The FormData instance to add the field to.
        key: The field name.
        value: The field value.
    """
    if value is True:
        form.add_field(key, "on")
    elif value is False or value is None:
        return
    elif isinstance(value, int):
        form.add_field(key, str(value))
    else:
        form.add_field(key, value)


def _compose_form_data(files: UploadFiles, data: dict[str, Any]) -> FormData:
    """Compose FormData by converting UploadFiles and adding data fields.

    Args:
        files: The UploadFiles object containing file uploads.
        data: Dictionary of field names and values to add.

    Returns:
        A new FormData object with all files and fields added.
    """
    form = FormData()
    form.add_field(
        "file_input",
        files.torrent_data,
        filename="meowmeow.torrent",
        content_type="application/octet-stream",
    )
    for log_name, log_data in files.log_files:
        form.add_field(
            "logfiles[]",
            log_data,
            filename=log_name,
            content_type="application/octet-stream",
        )
    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                _add_form_field(form, key, item)
        else:
            _add_form_field(form, key, value)
    return form


class SearchReleaseData(msgspec.Struct, frozen=True):
    """Data structure for search release results."""

    lossless: bool
    lossless_web: bool
    year: int | None
    artist: str
    album: str
    release_type: str | int
    url: str


# The tracker limits the account, so every client of one tracker shares a budget. Per event
# loop: an AsyncLimiter binds to the first loop that uses it, and web UI jobs run their own.
_limiters: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, AsyncLimiter]] = WeakKeyDictionary()


def _tracker_limiter(site_code: str) -> AsyncLimiter:
    """5 requests per 10 seconds for one tracker on the running loop."""
    per_loop = _limiters.setdefault(asyncio.get_running_loop(), {})
    if site_code not in per_loop:
        per_loop[site_code] = AsyncLimiter(5, 10)
    return per_loop[site_code]


# An upload that fills a request takes two hops; one spare.
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset(
    {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
    }
)
# The request never left, so re-sending it is safe whatever it does.
_NOT_SENT_ERRORS = (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError)
_TRANSIENT_5XX = frozenset(
    {
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }
)


# Pools still open per click context, closed when it exits. A set, not one callback per pool, so a
# long-lived context (the web UI server's) holds nothing for a pool already closed.
_open_pools: WeakKeyDictionary[click.Context, set[aiohttp.ClientSession]] = WeakKeyDictionary()
# Web UI job threads register their own contexts concurrently.
_open_pools_lock = threading.Lock()


def _close_with_context(session: aiohttp.ClientSession) -> set[aiohttp.ClientSession] | None:
    """Close ``session`` when the current click context exits; returns that context's open-pool set."""
    # Web UI jobs get their own context in JobManager._thread_main.
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return None
    with _open_pools_lock:
        open_pools = _open_pools.get(ctx)
        if open_pools is None:
            open_pools = _open_pools[ctx] = set()

            async def close_open_pools() -> None:
                for pool in list(open_pools):
                    await pool.close()

            ctx.call_on_close(close_open_pools)
    open_pools.add(session)
    return open_pools


class RetryableError(RequestError):
    """Exception for retryable network errors."""

    pass


class HttpResponse(msgspec.Struct, frozen=True):
    """HTTP response data extracted from aiohttp.ClientResponse."""

    text: str
    url: str
    status: int


class BaseGazelleApi:
    """Base API client for Gazelle-based trackers."""

    # Subclasses must set these attributes before calling __init__
    cookie: str
    base_url: str
    tracker_url: str
    site_code: str
    site_string: str
    api_key: str = ""  # Optional, only for API key upload
    api_key_prefix: str = ""  # OPS wants "token <key>"; RED wants the bare key
    keeplogged: str | None = None
    dry_run: bool = False  # when True, validate the upload but never actually send it

    def __init__(self) -> None:
        """Initialize the API client. Subclasses should call this after setting cookie/base_url."""
        self.headers = {
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "User-Agent": cfg.upload.user_agent,
        }
        if not hasattr(self, "dot_torrents_dir"):
            self.dot_torrents_dir = cfg.directory.dottorrents_dir

        self.release_types = RELEASE_TYPES
        self.authkey: str | None = None
        self.passkey: str | None = None
        self._authenticated = False
        self._session: aiohttp.ClientSession | None = None
        self._pool_owner: set[aiohttp.ClientSession] | None = None

    @property
    def _rate_limiter(self) -> AsyncLimiter:
        """This tracker's request budget on the running event loop."""
        return _tracker_limiter(self.site_code)

    def _http_session(self) -> aiohttp.ClientSession:
        """Get this instance's kept-alive pool, opening it on first use."""
        if self._session is None or self._session.closed:
            # Two reused connections, so a gathered batch can't open one TLS handshake per request
            # and read as scanner traffic. DummyCookieJar: cookies go per request, never kept.
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=2),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
            self._pool_owner = _close_with_context(self._session)
        return self._session

    async def close(self) -> None:
        """Close the kept-alive pool."""
        if self._session is not None:
            await self._session.close()
            # Only this pool's own context, never the shared registry another thread may be adding to.
            if self._pool_owner is not None:
                self._pool_owner.discard(self._session)
                self._pool_owner = None
            self._session = None

    @asynccontextmanager
    async def _session_for(self, idempotent: bool) -> AsyncIterator[aiohttp.ClientSession]:
        """The pool, or a one-off fresh connection for a request that must not be re-sent."""
        if idempotent:
            yield self._http_session()
            return
        # A pooled connection may be one the tracker is closing as idle, and a failure on it
        # can't be told from a request the tracker acted on.
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(force_close=True),
            cookie_jar=aiohttp.DummyCookieJar(),
        ) as session:
            yield session

    @asynccontextmanager
    async def site_get(
        self, url: str, headers: dict[str, str] | None = None, timeout_secs: int = 30
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """GET a raw file off the tracker, inside its rate limit, without following redirects."""
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=timeout_secs, sock_read=timeout_secs)
        async with (
            self._rate_limiter,
            self._http_session().get(
                url,
                headers={**self.headers, **(headers or {})},
                cookies=self._get_cookies(),
                timeout=timeout,
                allow_redirects=False,
            ) as resp,
        ):
            # sock_read resets on every chunk, so a trickled body needs its own bound (as in _request).
            async with asyncio.timeout(timeout_secs):
                yield resp

    def _get_cookies(self) -> dict[str, str]:
        """Get cookies dict for requests."""
        return _build_tracker_cookies(self.cookie, self.keeplogged)

    @property
    def announce(self) -> str:
        """Get the announce URL."""
        return f"{self.tracker_url}/{self.passkey}/announce"

    def request_url(self, id: int) -> str:
        """Get URL for a request page.

        Args:
            id: The request ID.

        Returns:
            The request URL.
        """
        return f"{self.base_url}/requests.php?action=view&id={id}"

    async def authenticate(self) -> None:
        """Authenticate with the tracker API and get authkey/passkey."""
        acctinfo = await self.api_call("index")
        self.authkey = acctinfo["authkey"]
        self.passkey = acctinfo["passkey"]
        self._authenticated = True

    async def ensure_authenticated(self) -> None:
        """Ensure we are authenticated before making requests."""
        if not self._authenticated:
            await self.authenticate()

    @retry(
        retry=retry_if_exception_type(RetryableError),
        stop=stop_after_attempt(5),
        # Jitter keeps a batch that failed together from retrying in one salvo.
        wait=wait_exponential(multiplier=1, min=1, max=30) + wait_random(0, 2),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        data: Any = None,
        timeout_secs: int = 10,
        prefer_api_key: bool = False,
        idempotent: bool | None = None,
    ) -> HttpResponse:
        """Authenticated HTTP request, returns response data.

        Args:
            method: HTTP method (e.g. "GET", "POST").
            url: The URL to request.
            params: Query parameters.
            data: POST body data.
            timeout_secs: Request timeout in seconds.
            prefer_api_key: If True and api_key is set, use Authorization header
                only (no cookie). If False or api_key is empty, use cookie only
                (no Authorization header).
            idempotent: Whether sending the request twice does no more than sending it
                once. Defaults to True for everything but POST.

        Returns:
            HttpResponse with text, url, status, and headers.

        Raises:
            UnknownOutcomeError: A request that is not idempotent failed after it may have
                reached the tracker, so it is not sent again.
        """
        if idempotent is None:
            idempotent = method.upper() != "POST"
        # Once the tracker redirects, it has acted on the request, whatever happens next.
        redirected = False

        def failure(message: str, *, not_acted_on: bool = False) -> RequestError:
            # Re-sending is only safe when repeating the request cannot change anything twice.
            if idempotent or (not_acted_on and not redirected):
                return RetryableError(message)
            return UnknownOutcomeError(message)

        if not (params and params.get("action") == "index"):
            await self.ensure_authenticated()

        use_api_key = prefer_api_key and bool(self.api_key)
        headers = {**self.headers, **({"Authorization": f"{self.api_key_prefix}{self.api_key}"} if use_api_key else {})}
        cookies = {} if use_api_key else self._get_cookies()

        if cfg.upload.debug_tracker_connection:
            click.secho(f"[DEBUG] {method} {url}", fg="cyan")
            click.secho(f"[DEBUG] params: {_redact(msgspec.json.encode(params).decode())}", fg="cyan")
            click.secho(f"[DEBUG] use_api_key: {use_api_key}", fg="cyan")

        try:
            # No total: it would also count the wait for a free pooled connection.
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=timeout_secs, sock_read=timeout_secs)
            async with self._session_for(idempotent) as session:
                # Hop by hop, each through the limiter: aiohttp would follow them all in one slot (#432).
                for _ in range(_MAX_REDIRECTS + 1):
                    async with (
                        self._rate_limiter,
                        session.request(
                            method,
                            url,
                            params=params,
                            data=data,
                            headers=headers,
                            cookies=cookies,
                            timeout=timeout,
                            allow_redirects=False,
                        ) as resp,
                    ):
                        # sock_read resets on every chunk, so a trickled body needs its own bound.
                        async with asyncio.timeout(timeout_secs):
                            text = await resp.text()
                        if cfg.upload.debug_tracker_connection:
                            self._debug_response(resp, text)
                        if not resp.ok:
                            await self._raise_for_status(resp, text, failure, idempotent)
                        location = resp.headers.get(aiohttp.hdrs.LOCATION)
                        if resp.status not in _REDIRECT_STATUSES or not location:
                            return HttpResponse(text=text, url=str(resp.url), status=resp.status)
                        try:
                            method, data, url = self._next_hop(str(resp.url), resp.status, method, data, location)
                        except RequestFailedError:
                            # Refusing an off-site hop doesn't undo the redirect: the tracker has acted.
                            redirected = True
                            raise
                        params = None
                        redirected = True

            click.secho(f"Too many redirects from {self.site_string}, last to {urlparse(url).path}", fg="red")
            raise RequestFailedError(f"Too many redirects from {self.site_string}")
        except (TimeoutError, aiohttp.ClientError) as err:
            # Checked by type: ConnectionTimeoutError is also a TimeoutError, and never connected is safe to resend.
            raise failure(f"Network error: {err}", not_acted_on=isinstance(err, _NOT_SENT_ERRORS)) from err
        except RequestError as err:
            # After a redirect the tracker has acted, so a refused later hop is an unknown outcome.
            if idempotent or not redirected or isinstance(err, UnknownOutcomeError):
                raise
            # By type only: a RequestFailedError carries the raw response body.
            raise UnknownOutcomeError(f"{self.site_string} failed on a later hop ({type(err).__name__})") from err

    @staticmethod
    def _debug_response(resp: aiohttp.ClientResponse, text: str) -> None:
        """Print a tracker answer, redacted, for debug_tracker_connection."""
        click.secho(f"[DEBUG] status: {resp.status}", fg="cyan")
        click.secho(
            f"[DEBUG] response headers: {_redact(msgspec.json.encode(dict(resp.headers)).decode())}",
            fg="cyan",
        )
        click.secho(f"[DEBUG] response body: {_redact(text)}", fg="green")

    async def _raise_for_status(
        self, resp: aiohttp.ClientResponse, text: str, failure: Callable[..., RequestError], idempotent: bool
    ) -> NoReturn:
        """Raise the error a failed tracker answer calls for."""
        error_msg = text
        with suppress(msgspec.DecodeError, ValueError):
            error_msg = msgspec.json.encode(msgspec.json.decode(text)["error"]).decode()

        if resp.status == HTTPStatus.TOO_MANY_REQUESTS or "rate limit" in error_msg.lower():
            retry_after = float(resp.headers.get("Retry-After", "20"))
            click.secho(f"Rate limit exceeded, waiting {retry_after} seconds...", fg="yellow")
            await asyncio.sleep(retry_after)
            raise failure("Rate limit exceeded", not_acted_on=True)

        if resp.status == HTTPStatus.UNAUTHORIZED:
            click.secho(
                f"Authentication to {self.site_string} failed: {error_msg}.\nYour API key may be invalid.",
                fg="red",
            )
            raise LoginError(error_msg)

        # Any 5xx may follow the tracker acting on a POST; a GET is resent only on these.
        if resp.status >= HTTPStatus.INTERNAL_SERVER_ERROR and (not idempotent or resp.status in _TRANSIENT_5XX):
            raise failure(f"Server error {resp.status}")

        click.secho(f"Request to {self.site_string} failed ({resp.status}): {error_msg}", fg="red")
        raise RequestFailedError(error_msg)

    def _next_hop(self, current: str, status: int, method: str, data: Any, location: str) -> tuple[str, Any, str]:
        """The method, body and URL a tracker redirect sends a request on to."""
        target = urlparse(urljoin(current, location))
        if target.path.endswith("/login.php"):
            click.secho(
                f"{self.site_string} sent this request to its login page: your session cookie is missing or "
                f"expired. Check tracker.{self.site_code.lower()}.session in your config.",
                fg="red",
                bold=True,
            )
            raise LoginError(f"{self.site_string} redirected to its login page")
        origin = urlparse(current)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
            click.secho(f"{self.site_string} redirected to {target.scheme}://{target.netloc}, not following.", fg="red")
            raise RequestFailedError(f"{self.site_string} redirected to another site")
        # As browsers and aiohttp do, a redirected POST is fetched with GET.
        if status == HTTPStatus.SEE_OTHER or (
            status in (HTTPStatus.MOVED_PERMANENTLY, HTTPStatus.FOUND) and method.upper() == "POST"
        ):
            method, data = "GET", None
        return method, data, target._replace(fragment="").geturl()

    async def api_call(self, action: str, params: dict[str, Any] | None = None) -> dict:
        """Make a request to the site API with rate limiting.

        Args:
            action: The API action to perform.
            params: Additional parameters for the request.

        Returns:
            The API response data.

        Raises:
            LoginError: If authentication fails.
            RequestFailedError: If the request fails.
            RetryableError: If network error persists after retries
                (a RequestError subclass).
        """
        url = self.base_url + "/ajax.php"
        params = {"action": action, **(params or {})}

        resp = await self._request("GET", url, params=params, timeout_secs=5, prefer_api_key=True)

        try:
            resp_json = msgspec.json.decode(resp.text)
        except (msgspec.DecodeError, ValueError):
            resp_json = {"status": "error", "error": resp.text}

        if resp_json.get("status") != "success":
            raise RequestFailedError(str(resp_json.get("error", resp.text)))
        return cast("dict", resp_json["response"])

    async def torrentgroup(self, group_id: int) -> dict:
        """Get information about a torrent group.

        Args:
            group_id: The torrent group ID.

        Returns:
            The torrent group data.
        """
        return await self.api_call("torrentgroup", params={"id": group_id})

    async def get_redirect_torrentgroupid(self, torrentid: int) -> int | None:
        """Get torrent group ID from torrent ID via redirect.

        Args:
            torrentid: The torrent ID.

        Returns:
            The torrent group ID as int, or None if not found.
        """
        url = self.base_url + "/torrents.php"
        try:
            resp = await self._request("GET", url, params={"torrentid": torrentid}, timeout_secs=5)
        except TimeoutError:
            click.secho("Connection to API timed out, try script again later. Gomen!", fg="red")
            raise click.Abort() from None
        parsed = urlparse(resp.url)
        query = parse_qs(parsed.query)
        group_id = query.get("id", [None])[0]
        if group_id:
            return int(group_id)
        click.secho("Couldn't retrieve torrent_group_id from torrent_id, no Redirect found!", fg="red")
        raise click.Abort()

    async def get_request(self, id: int) -> dict:
        """Get information about a request.

        Args:
            id: The request ID.

        Returns:
            The request data.
        """
        return await self.api_call("request", params={"id": id})

    async def artist_rls(self, artist: str):
        """Get all torrent groups belonging to an artist.

        Args:
            artist: The artist name.

        Returns:
            Tuple of (artist_id, list of releases).
        """
        resp = await self.api_call("artist", params={"artistname": artist})
        releases = []
        for group in resp["torrentgroup"]:
            # We do not put compilations or guest appearances in this list.
            if not group["artists"]:
                continue
            if group["releaseType"] == 7 and (
                not group["extendedArtists"]["6"]
                or artist.lower() not in {a["name"].lower() for a in group["extendedArtists"]["6"]}
            ):
                continue
            if group["releaseType"] in {1023, 1021, 1022, 1024}:
                continue

            releases.append(
                SearchReleaseData(
                    lossless=any(t["format"] == "FLAC" for t in group["torrent"]),
                    lossless_web=any(t["format"] == "FLAC" and t["media"] == "WEB" for t in group["torrent"]),
                    year=group["groupYear"],
                    artist=html.unescape(compile_artists(group["artists"], group["releaseType"])),
                    album=html.unescape(group["groupName"]),
                    release_type=INVERTED_RELEASE_TYPES[group["releaseType"]],
                    url=f"{self.base_url}/torrents.php?id={group['groupId']}",
                )
            )

        releases = list({r.url: r for r in releases}.values())  # Dedupe

        return resp["id"], releases

    async def label_rls(self, label, year=None):
        """
        Get all the torrent groups from a label on site.
        All groups without a FLAC will be highlighted.
        """
        browse_params = {"remasterrecordlabel": label}
        if year:
            browse_params["year"] = year
        first_request = await self.api_call("browse", params=browse_params)
        if "pages" in first_request:
            pages = first_request["pages"]
        else:
            return []
        all_results = first_request["results"]
        # Hits to the site are slow because of rate limiting.
        # Should probably be spun out into its own pagination function at some point.
        for i in range(2, pages + 1):
            browse_params["page"] = str(i)
            new_results = await self.api_call("browse", params=browse_params)
            all_results += new_results["results"]
        releases = []
        for group in all_results:
            if not group["artist"]:
                if "artists" in group:
                    artist = html.unescape(compile_artists(group["artists"], group["releaseType"]))
                else:
                    artist = ""
            else:
                artist = group["artist"]
            releases.append(
                SearchReleaseData(
                    lossless=any(t["format"] == "FLAC" for t in group["torrents"]),
                    lossless_web=any(t["format"] == "FLAC" and t["media"] == "WEB" for t in group["torrents"]),
                    year=group["groupYear"],
                    artist=artist,
                    album=html.unescape(group["groupName"]),
                    release_type=group["releaseType"],
                    url=f"{self.base_url}/torrents.php?id={group['groupId']}",
                )
            )

        releases = list({r.url: r for r in releases}.values())  # Dedupe

        return releases

    async def fetch_log(self, page: int) -> str:
        """Fetch a page of the site log.

        Args:
            page: The page number.

        Returns:
            The page HTML text.
        """
        url = f"{self.base_url}/log.php"
        resp = await self._request("GET", url, params={"page": page})
        return resp.text

    async def fetch_riplog(self, torrentid: int) -> str:
        """Fetch rip log for a torrent.

        Args:
            torrentid: The torrent ID.

        Returns:
            The log text with some content stripped.
        """
        url = f"{self.base_url}/torrents.php"
        resp = await self._request("GET", url, params={"action": "loglist", "torrentid": torrentid})
        return re.sub(r" ?\([^)]+\)", "", resp.text)

    async def get_uploads_from_log(self, max_pages: int = 10) -> list:
        """Crawl log pages and return uploads.

        Args:
            max_pages: Maximum number of pages to crawl.

        Returns:
            List of (torrent_id, artist, title) tuples.
        """
        # log.php is a site page: an API key doesn't open it, and without a cookie it bounces to login.
        if not self.cookie.strip():
            click.secho(
                f"Skipping the recent-uploads check: the {self.site_string} site log needs a session cookie "
                f"(tracker.{self.site_code.lower()}.session), and none is set.",
                fg="yellow",
            )
            return []
        # Probe page 1 alone: an invalid cookie must not fan out into N redirect chains (#432)
        try:
            first_page = await self.fetch_log(1)
        except (LoginError, RequestError) as e:
            click.secho(
                f"Skipping the recent-uploads check: could not read the {self.site_string} site log ({e}). "
                "This check requires a valid session cookie.",
                fg="yellow",
                bold=True,
            )
            return []
        recent_uploads = self.parse_uploads_from_log_html(first_page)
        tasks = [self.fetch_log(i) for i in range(2, max_pages + 1)]
        # gather does not cancel siblings on error; tolerate a mid-crawl failure (#432)
        for page_text in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(page_text, str):
                recent_uploads += self.parse_uploads_from_log_html(page_text)
        return recent_uploads

    async def api_key_upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via API.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).

        Raises:
            RequestError: If upload fails.
            UploadError: If the site reports success but no torrent id.
        """
        url = self.base_url + "/ajax.php?action=upload"
        data["auth"] = self.authkey

        try:
            response = await self._request(
                "POST", url, data=_compose_form_data(files, data), timeout_secs=30, prefer_api_key=True
            )
        except UnknownOutcomeError as err:
            return await self._find_lost_upload(files, err)
        try:
            resp = msgspec.json.decode(response.text)
        except (msgspec.DecodeError, ValueError) as e:
            click.secho("❌ Failed to decode JSON response", fg="red", err=True)
            click.secho(f"Status code: {response.status}", fg="red", err=True)
            click.secho(f"Response text: {_safe_response_excerpt(response.text)}", fg="red", err=True)
            raise click.Abort from e

        try:
            if resp["status"] != "success":
                raise RequestError(f"API upload failed: {resp.get('error', resp)}")
            if ("requestid" in resp["response"] and resp["response"]["requestid"]) or (
                "fillRequest" in resp["response"]
                and resp["response"]["fillRequest"]
                and resp["response"]["fillRequest"]["requestId"]
            ):
                requestId = (
                    resp["response"]["requestid"]
                    if "requestid" in resp["response"]
                    else resp["response"]["fillRequest"]["requestId"]
                )
                if requestId == -1:
                    click.secho("Request fill failed!", fg="red")
                else:
                    click.secho("Filled request: " + self.request_url(requestId), fg="green")
            torrent_id = 0
            group_id = 0
            if "torrentid" in resp["response"]:
                torrent_id = resp["response"]["torrentid"]
                group_id = resp["response"]["groupid"]
            elif "torrentId" in resp["response"]:
                torrent_id = resp["response"]["torrentId"]
                group_id = resp["response"]["groupId"]
            elif "requestid" not in resp["response"] and "fillRequest" not in resp["response"]:
                raise UploadError(f"API upload succeeded but returned no torrent id, response: {resp}")
            return torrent_id, group_id
        except TypeError as err:
            raise RequestError(f"API upload failed, response: {resp}") from err

    async def site_page_upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via upload.php.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).

        Raises:
            RequestError: If upload fails.
        """
        if "groupid" in data:
            url = self.base_url + f"/upload.php?groupid={data['groupid']}"
        else:
            url = self.base_url + "/upload.php"
        data["auth"] = self.authkey

        try:
            response = await self._request("POST", url, data=_compose_form_data(files, data), timeout_secs=30)
        except UnknownOutcomeError as err:
            return await self._find_lost_upload(files, err)
        resp_text = response.text
        resp_url = response.url

        if self.announce in resp_text:
            match = re.search(r'<p style="color: red; text-align: center;">(.+)<\/p>', resp_text)
            if match:
                raise RequestError(f"Site upload failed: {match[1]} ({response.status})")
        if "requests.php" in resp_url:
            try:
                torrent_id = self.parse_torrent_id_from_filled_request_page(resp_text)
                group_id = await self.get_redirect_torrentgroupid(torrent_id) or 0
                click.secho(f"Filled request: {resp_url}", fg="green")
                return torrent_id, group_id
            except (TypeError, ValueError) as err:
                soup = BeautifulSoup(resp_text, "lxml")
                error = soup.find("h2", string="Error")  # pyright: ignore[reportCallIssue, reportArgumentType] - bs4 stubs reject name+string
                error_message = _safe_response_excerpt(resp_text)
                if error and error.parent and error.parent.parent:
                    p_tag = error.parent.parent.find("p")
                    if p_tag:
                        error_message = _redact(p_tag.text)
                raise RequestError(f"Request fill failed: {error_message}") from err
        try:
            return self.parse_most_recent_torrent_and_group_id_from_group_page(resp_text)
        except TypeError as err:
            raise RequestError(f"Site upload failed, response text: {_safe_response_excerpt(resp_text)}") from err

    async def _find_lost_upload(self, files: UploadFiles, err: UnknownOutcomeError) -> tuple[int, int]:
        """Look an upload whose answer was lost up once, by its infohash, and return its ids."""
        click.secho(f"Could not tell whether {self.site_string} took the upload ({err}), looking it up...", fg="yellow")
        try:
            # Gazelle's API documentation asks for the hash in uppercase.
            infohash = Torrent.read_stream(files.torrent_data).infohash.upper()
            found = await self.api_call("torrent", params={"hash": infohash})
            torrent_id, group_id = int(found["torrent"]["id"]), int(found["group"]["id"])
        except (RequestError, TorfError, KeyError, TypeError, ValueError) as lookup_err:
            # A non-JSON answer arrives here whole, and a logged-in page's links carry secrets.
            reason = _safe_response_excerpt(str(lookup_err))
            raise UnknownOutcomeError(
                f"Could not tell whether {self.site_string} took the upload ({err}), and looking it up by "
                f"its infohash did not confirm it ({reason}). The upload may still have gone through: "
                f"check your uploads on {self.site_string} before uploading it again."
            ) from lookup_err
        click.secho(f"Found the upload on {self.site_string}: torrent {torrent_id}.", fg="green")
        return torrent_id, group_id

    async def upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Upload torrent via API or upload.php.

        Args:
            data: Upload form data.
            files: UploadFiles containing files to upload.

        Returns:
            Tuple of (torrent_id, group_id).
        """
        if self.api_key:
            return await self.api_key_upload(data, files)

        return await self.site_page_upload(data, files)

    async def dry_run_upload(self, data: dict, files: UploadFiles) -> tuple[int, int]:
        """Build the upload locally and send nothing.

        Never call a tracker-side dryrun here: posting the form to validate it is
        still posting it, which is not what --dry-run promises.
        """
        click.secho(
            f"\n[DRY RUN] {self.site_string}: prepared the torrent and upload form locally. "
            f"Nothing was sent to {self.site_string}.",
            fg="cyan",
            bold=True,
        )
        return 0, 0

    async def report_lossy_master(self, torrent_id: int, comment: str, source: str) -> bool:
        """Report torrent for lossy master/web approval.

        Args:
            torrent_id: The torrent ID.
            comment: Report comment.
            source: Media source (e.g., "WEB").

        Returns:
            True if successful.

        Raises:
            RequestError: If report fails.
        """
        url = self.base_url + "/reportsv2.php"
        type_ = "lossywebapproval" if source == "WEB" else "lossyapproval"
        data = {
            "auth": self.authkey,
            "torrentid": torrent_id,
            "categoryid": 1,
            "type": type_,
            "extra": comment,
            "submit": True,
        }
        resp = await self._request("POST", url, params={"action": "takereport"}, data=data)
        if "torrents.php" in resp.url:
            return True
        raise RequestError(f"Failed to report the torrent for lossy master, code {resp.status}.")

    async def append_to_torrent_description(self, torrent_id: int, description_addition: str) -> None:
        """Add text to start of torrent description.

        Args:
            torrent_id: The torrent ID.
            description_addition: Text to prepend to description.

        Raises:
            RequestError: If edit fails.
        """
        current_details = await self.api_call("torrent", params={"id": torrent_id})
        new_data = {
            "action": "takeedit",
            "torrentid": torrent_id,
            "type": 1,
            "groupremasters": 0,
            "remaster_year": current_details["torrent"]["remasterYear"],
            "remaster_title": current_details["torrent"]["remasterTitle"],
            "remaster_record_label": current_details["torrent"]["remasterRecordLabel"],
            "remaster_catalogue_number": current_details["torrent"]["remasterCatalogueNumber"],
            "format": current_details["torrent"]["format"],
            "bitrate": current_details["torrent"]["encoding"],
            "other_bitrate": "",
            "media": current_details["torrent"]["media"],
            "release_desc": description_addition + current_details["torrent"]["description"],
            "auth": self.authkey,
        }
        url = self.base_url + "/torrents.php"
        # Every field is rebuilt from the torrent's current state, so sending it twice is harmless.
        resp = await self._request("POST", url, data=new_data, idempotent=True)
        resp_text = resp.text

        soup = BeautifulSoup(resp_text, "lxml")
        edit_error = soup.find("h2", string="Error")  # pyright: ignore[reportCallIssue, reportArgumentType] - bs4 stubs reject name+string
        if edit_error and edit_error.parent and edit_error.parent.parent:
            p_tag = edit_error.parent.parent.find("p")
            error_message = p_tag.text if p_tag else "Unknown error"
            raise RequestError(f"Failed to edit torrent: {error_message}")
        else:
            click.secho("Added spectrals to the torrent description.", fg="green")

    """The following three parsing functions are part of the gazelle class
    in order that they be easily overwritten in the derivative site classes.
    It is not because they depend on anything from the class"""

    def parse_most_recent_torrent_and_group_id_from_group_page(self, text: str) -> tuple[int, int]:
        """
        Given the HTML (ew) response from a successful upload, find the most
        recently uploaded torrent (it better be ours).
        """
        torrent_ids: list[int] = []
        group_ids: list[int] = []
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a", class_="tooltip"):
            href = pl.get("href", "")
            torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", str(href))
            if torrent_url:
                torrent_ids.append(int(torrent_url[1]))
        for pl in soup.find_all("a", class_="brackets"):
            href = pl.get("href", "")
            group_url = re.search(r"upload.php\?groupid=(\d+)", str(href))
            if group_url:
                group_ids.append(int(group_url[1]))

        if not torrent_ids or not group_ids:
            raise TypeError("Could not parse torrent/group id from group page")

        return max(torrent_ids), max(group_ids)

    def parse_torrent_id_from_filled_request_page(self, text: str) -> int:
        """
        Given the HTML (ew) response from filling a request,
        find the filling torrent (hopefully our upload)
        """
        torrent_ids: list[int] = []
        soup = BeautifulSoup(text, "lxml")
        for pl in soup.find_all("a"):
            if pl.string == "Yes":
                href = pl.get("href", "")
                torrent_url = re.search(r"torrents.php\?torrentid=(\d+)", str(href))
                if torrent_url:
                    torrent_ids.append(int(torrent_url[1]))
        return max(torrent_ids)

    def parse_uploads_from_log_html(self, text: str) -> list[tuple[str, str, str]]:
        """Parses a log page and returns best guess at
        (torrent id, 'Artist', 'title') tuples for uploads"""
        log_uploads: list[tuple[str, str, str]] = []
        soup = BeautifulSoup(text, "lxml")
        for entry in soup.find_all("span", class_="log_upload"):
            anchor = entry.find("a")
            if not anchor:
                continue
            href = anchor.get("href", "")
            torrent_id = str(href)[23:]
            try:
                # it having class log_upload is no guarantee that is what it is. Nice one log.
                next_sib = anchor.next_sibling
                if not next_sib:
                    continue
                torrent_string = re.findall(r"\((.*?)\) \(", str(next_sib))[0].split(" - ")
            except (IndexError, TypeError):
                continue
            artist = torrent_string[0]
            if len(torrent_string) > 1:
                title = torrent_string[1]
            else:
                artist = ""
                title = torrent_string[0]
            log_uploads.append((torrent_id, artist, title))
        return log_uploads


def compile_artists(artists, release_type):
    """Generate a string to represent the artists."""
    if release_type == 7 or len(artists) > 3:
        return cfg.upload.formatting.various_artist_word
    return " & ".join([a["name"] for a in artists])
