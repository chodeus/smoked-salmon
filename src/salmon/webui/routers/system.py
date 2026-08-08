"""System endpoints: health, binaries, configuration overview."""

import shutil
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter

from salmon import cfg
from salmon.config import find_config_path

router = APIRouter(tags=["system"])

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
