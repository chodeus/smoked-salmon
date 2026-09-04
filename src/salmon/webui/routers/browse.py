"""Filesystem browsing for folder selection in the UI."""

import os

from fastapi import APIRouter, HTTPException

from salmon import cfg
from salmon.webui.validation import allowed_roots, is_within_roots

router = APIRouter(tags=["browse"])

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a"}


@router.get("/browse")
def browse(path: str | None = None) -> dict:
    """List subdirectories and audio files of a directory.

    Defaults to the configured download directory and stays confined to
    salmon's configured directories (no browsing out to '/', $HOME or mounts).
    """
    if not path:
        path = cfg.directory.download_directory
    path = os.path.realpath(os.path.expanduser(path))
    # Confinement before the isdir probe: a 404 on an out-of-root path would say whether it exists.
    if not is_within_roots(path):
        raise HTTPException(status_code=403, detail="Refusing to browse outside the configured directories.")
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

    parent = os.path.dirname(path)
    return {
        "path": path,
        "parent": parent if is_within_roots(parent) else None,
        "dirs": dirs,
        "audio_files": audio_files,
        # Roots are siblings, not ancestors — without these the picker can only
        # ever reach whichever one it opened at.
        "roots": [
            {"path": r, "name": os.path.basename(r) or r, "library": cfg.directory.is_library_path(r)}
            for r in allowed_roots()
        ],
    }
