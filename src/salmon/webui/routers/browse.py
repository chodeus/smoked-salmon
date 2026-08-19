"""Filesystem browsing for folder selection in the UI."""

import os

from fastapi import APIRouter, HTTPException

from salmon import cfg

router = APIRouter(tags=["browse"])

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a"}


@router.get("/browse")
def browse(path: str | None = None) -> dict:
    """List subdirectories and audio files of a directory.

    Defaults to the configured download directory.
    """
    if not path:
        path = cfg.directory.download_directory
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail=f"Not a directory: {path}")

    dirs = []
    audio_files = []
    try:
        with os.scandir(path) as entries:
            for entry in sorted(entries, key=lambda e: e.name.lower()):
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    dirs.append({"name": entry.name, "path": entry.path})
                elif os.path.splitext(entry.name.lower())[1] in AUDIO_EXTENSIONS:
                    audio_files.append(entry.name)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    return {
        "path": path,
        "parent": os.path.dirname(path) if path != "/" else None,
        "dirs": dirs,
        "audio_files": audio_files,
    }
