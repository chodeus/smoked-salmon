"""Shared test setup.

Importing ``salmon`` parses the config at import time and exits if none is
found. To keep the test suite hermetic (CI, fresh checkouts), a minimal config
is generated in a temporary XDG_CONFIG_HOME before the first salmon import —
unless a developer config.toml exists at the repo root, which takes precedence
in salmon's own discovery order.
"""

import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _ensure_config() -> None:
    if (_REPO_ROOT / "config.toml").exists():
        return
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
        f'[tracker.red]\nsession = "test-session"\n\n[tracker]\ndefault_tracker = "RED"\n'
    )


_ensure_config()

import pytest  # noqa: E402

from salmon.errors import RequestError  # noqa: E402


class FakeGazelleApi:
    """A minimal stand-in for BaseGazelleApi used by uploader-level tests.

    Configure per-test via the ``api_responses`` dict (keyed by ajax action)
    and the ``upload_result`` / ``upload_error`` attributes.
    """

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
    """A fake album folder with dummy audio bytes (enough for hashing/paths)."""
    album = tmp_path / "Testartist - Testalbum (2024) [FLAC]"
    album.mkdir()
    for i, title in enumerate(["Intro", "Mittelteil", "Outro"], start=1):
        (album / f"{i:02d}. {title}.flac").write_bytes(b"fLaC" + bytes(2000))
    return album
