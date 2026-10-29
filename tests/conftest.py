"""Shared test setup.

Importing ``salmon`` parses the config at import time and exits if none is
found. To keep the test suite hermetic (CI, fresh checkouts), a minimal config
is generated in a temporary XDG_CONFIG_HOME before the first salmon import.

This also keeps a developer's real config out of the test run: it sets
``SALMON_CONFIG_DIR`` to that temporary location, which ``find_config_path()``
prefers over a repo-root ``config.toml`` (a documented dev convenience) since
an explicitly set ``SALMON_CONFIG_DIR`` beats that implicit fallback.
"""

import ipaddress
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _ensure_config() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="salmon-test-config-"))
    os.environ["XDG_CONFIG_HOME"] = str(tmp)
    cfg_dir = tmp / "smoked-salmon"
    cfg_dir.mkdir(parents=True)
    music = tmp / "music"
    torrents = tmp / "torrents"
    tmpdir = tmp / "tmp"
    for d in (music, torrents, tmpdir):
        d.mkdir()
    (cfg_dir / "config.toml").write_text(
        f'[directory]\ndownload_directory = "{music}"\ndottorrents_dir = "{torrents}"\ntmp_dir = "{tmpdir}"\n\n'
        f'[tracker.red]\nsession = "test-session"\n\n[tracker]\ndefault_trackers = ["RED"]\n'
    )
    os.environ["SALMON_CONFIG_DIR"] = str(cfg_dir)


_ensure_config()

import pytest  # noqa: E402

from salmon.errors import RequestError  # noqa: E402


class FakeGazelleApi:
    """A minimal stand-in for BaseGazelleApi used by uploader-level tests.

    Configure per-test via the ``api_responses`` dict (keyed by ajax action)
    and the ``upload_result`` / ``upload_error`` attributes.
    """

    unsupported_artist_roles: frozenset[str] = frozenset()

    def upload_form_fields(self, metadata, track_data) -> dict[str, str]:
        return {}

    def __init__(self):
        self.site_code = "RED"
        self.site_string = "RED"
        self.base_url = "https://redacted.sh"
        self.announce = "https://flacsfor.me/test-passkey/announce"
        self.dot_torrents_dir = None  # set by tests that write torrents
        self.api_responses: dict[str, object] = {}
        self.api_calls: list[tuple[str, dict]] = []
        self.upload_result: tuple[int, int] = (1001, 2002)
        self.upload_error: Exception | None = None
        self.uploads: list[tuple[dict, object]] = []
        self.lossy_reports: list[tuple[int, str, str]] = []

    async def api_call(self, action: str, params: dict | None = None, **kwargs):
        self.api_calls.append((action, params or kwargs))
        if action not in self.api_responses:
            raise RequestError(f"no fake response configured for action={action}")
        response = self.api_responses[action]
        if isinstance(response, Exception):
            raise response
        return response

    async def upload(self, data: dict, files):
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append((data, files))
        return self.upload_result

    async def report_lossy_master(self, torrent_id: int, comment: str, source: str):
        self.lossy_reports.append((torrent_id, comment, source))

    async def torrentgroup(self, group_id: int):
        return await self.api_call("torrentgroup", {"id": group_id})

    def request_url(self, id: int) -> str:
        return f"{self.base_url}/requests.php?action=view&id={id}"


@pytest.fixture
def fake_tracker() -> FakeGazelleApi:
    return FakeGazelleApi()


@pytest.fixture
def album_dir(tmp_path: Path) -> Path:
    """A fake album folder with dummy audio bytes (enough for hashing/paths).

    Created under the configured download_directory (in a per-test subfolder)
    so it passes the webui's path-confinement check and mirrors reality.
    """
    from salmon import cfg

    # realpath so the path matches the webui's confinement resolution (macOS /var -> /private/var)
    base = Path(os.path.realpath(cfg.directory.download_directory)) / tmp_path.name
    base.mkdir(parents=True, exist_ok=True)
    album = base / "Testartist - Testalbum (2024) [FLAC]"
    album.mkdir()
    for i, title in enumerate(["Intro", "Mittelteil", "Outro"], start=1):
        (album / f"{i:02d}. {title}.flac").write_bytes(b"fLaC" + bytes(2000))
    return album


@pytest.fixture(autouse=True)
def _no_lost_upload_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Look a lost upload up without waiting, except in the tests that time the waits."""
    import salmon.trackers.base as tracker_base

    monkeypatch.setattr(tracker_base, "_LOST_UPLOAD_FIRST_WAIT", 0)
    monkeypatch.setattr(tracker_base, "_LOST_UPLOAD_SECOND_WAIT", 0)


# --- Network guard -----------------------------------------------------------
# A test that reaches a real service (an image host, a store, a tracker) is a
# bug: it must run against a local fake. Refuse every connection that is not to
# loopback or a unix socket, at the socket level so aiohttp, requests, urllib3
# and httpx are all covered. Tests that truly need the network opt out with
# ``@pytest.mark.network``; CI deselects them.
#
# ``socket.getaddrinfo`` is not guarded on purpose: a name only a local fake
# proxy resolves (tests/test_trackers_proxy.py) must keep working, aiohttp may
# resolve through c-ares instead, and the connect guard names the address anyway.


class NetworkBlockedError(OSError):
    """A test tried to connect to a non-loopback address."""


_network_allowed = False
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_local(sock: socket.socket, address: Any) -> bool:
    if sock.family == socket.AF_UNIX:
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _check(sock: socket.socket, address: Any) -> None:
    if _network_allowed or _is_local(sock, address):
        return
    parts = tuple(address) if isinstance(address, tuple) else ()
    target = ":".join(str(part) for part in parts[:2]) or repr(address)
    raise NetworkBlockedError(
        f"test tried to connect to {target}, which is not loopback. Give the test a local fake "
        "server, or mark it @pytest.mark.network if it really needs the network."
    )


def _guarded_connect(self: socket.socket, address, /):
    _check(self, address)
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address, /):
    _check(self, address)
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _network_guard(request: pytest.FixtureRequest):
    global _network_allowed
    _network_allowed = request.node.get_closest_marker("network") is not None
    try:
        yield
    finally:
        _network_allowed = False
