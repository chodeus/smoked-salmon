"""Shared request validation for webui routers."""

import os
from pathlib import Path

from fastapi import HTTPException


def validate_album_dir(raw_path: str) -> str:
    """Resolve a user-supplied directory and reject obviously wrong targets.

    Jobs walk the given directory recursively; the filesystem root or a user's
    home directory would sweep the whole system (a real incident: a mis-click
    in the folder picker started an integrity check over 16k files on ``/``).
    """
    path = os.path.abspath(os.path.expanduser(raw_path))
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail=f"Not a directory: {path}")
    if path == "/" or path == str(Path.home()):
        raise HTTPException(
            status_code=422,
            detail=f"Refusing to operate on {path} — pick a specific album folder.",
        )
    return path
