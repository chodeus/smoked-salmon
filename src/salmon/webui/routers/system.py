"""System endpoints: health, binaries, configuration overview, debugging."""

import asyncio
import math
import shutil
import sys
import threading
import time
import traceback
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, HTTPException, Request

import salmon.trackers
from salmon import cfg
from salmon.checks.connection import check_tracker_connection
from salmon.config import find_config_path

router = APIRouter(tags=["system"])


@router.get("/debug/threads")
def debug_threads(request: Request) -> dict:
    """Stack traces of all threads — for diagnosing hung jobs. Dev-only."""
    if not getattr(request.app.state, "dev", False):
        raise HTTPException(status_code=404, detail="Not found.")
    names = {t.ident: t.name for t in threading.enumerate()}
    return {
        str(names.get(ident, ident)): traceback.format_stack(frame) for ident, frame in sys._current_frames().items()
    }


REQUIRED_BINARIES = ["sox", "flac", "lame", "mp3val", "curl"]
OPTIONAL_BINARIES = ["rclone", "feh", "puddletag"]


@router.get("/health")
def health() -> dict:
    try:
        pkg_version = version("salmon")
    except PackageNotFoundError:
        pkg_version = "unknown"

    from salmon.trackers import tracker_list

    return {
        "version": pkg_version,
        "config_path": str(find_config_path()),
        "binaries": {
            "required": {name: shutil.which(name) for name in REQUIRED_BINARIES},
            "optional": {name: shutil.which(name) for name in OPTIONAL_BINARIES},
        },
        "trackers": tracker_list,
        "default_tracker": cfg.tracker.default_tracker,
        "directories": {
            "download": cfg.directory.download_directory,
            "tmp": cfg.directory.tmp_dir,
            "dottorrents": cfg.directory.dottorrents_dir,
        },
    }


# The dashboard runs this on load, so cache it: each call costs two live requests
# per tracker, and reloading the page should not keep hitting the sites.
CHECKCONF_TTL_SECONDS = 300
# monotonic: a backwards wall-clock jump must not keep a stale result alive.
_checkconf_cache: dict[str, object] = {"at": -math.inf, "result": None}
# One probe at a time, so concurrent dashboard loads share a single refresh.
_checkconf_lock = asyncio.Lock()


def _cached_checkconf(force: bool) -> dict | None:
    cached = _checkconf_cache["result"]
    if cached is None or force:
        return None
    age = time.monotonic() - float(_checkconf_cache["at"])  # type: ignore[arg-type]
    if age >= CHECKCONF_TTL_SECONDS:
        return None
    return {**cached, "cached": True, "age_seconds": int(age)}  # type: ignore[dict-item]


@router.post("/checkconf")
async def checkconf(force: bool = False) -> dict:
    """Test every configured tracker's session cookie and API key."""
    if (hit := _cached_checkconf(force)) is not None:
        return hit
    seen_at = _checkconf_cache["at"]
    async with _checkconf_lock:
        # If someone refreshed while we queued, their result is newer than this
        # request, so it satisfies a forced refresh too. Only a force that saw no
        # such refresh goes on to probe.
        if _checkconf_cache["at"] != seen_at and (hit := _cached_checkconf(force=False)) is not None:
            return hit
        results = [await check_tracker_connection(code) for code in salmon.trackers.tracker_list]
        payload = {
            "ok": all(r["session_ok"] and (r["api_key_ok"] is not False) for r in results),
            "trackers": results,
        }
        _checkconf_cache["at"] = time.monotonic()
        _checkconf_cache["result"] = payload
        return {**payload, "cached": False, "age_seconds": 0}
