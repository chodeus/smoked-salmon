"""System endpoints: health, binaries, configuration overview, debugging."""

import shutil
import sys
import threading
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
        str(names.get(ident, ident)): traceback.format_stack(frame)
        for ident, frame in sys._current_frames().items()
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


@router.post("/checkconf")
async def checkconf() -> dict:
    """Test every configured tracker's session cookie and API key."""
    results = [await check_tracker_connection(code) for code in salmon.trackers.tracker_list]
    ok = all(r["session_ok"] and (r["api_key_ok"] is not False) for r in results)
    return {"ok": ok, "trackers": results}
