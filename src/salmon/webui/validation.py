"""Shared request validation for webui routers."""

import os

from fastapi import HTTPException

from salmon import cfg


def allowed_roots() -> list[str]:
    """Real paths the UI may operate within: salmon's configured directories."""
    raw = [cfg.directory.download_directory, cfg.directory.dottorrents_dir, cfg.directory.tmp_dir]
    return [os.path.realpath(os.path.expanduser(r)) for r in raw if r]


def is_within_roots(path: str, roots: list[str] | None = None) -> bool:
    roots = allowed_roots() if roots is None else roots
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def validate_album_dir(raw_path: str) -> str:
    """Resolve a user-supplied directory and confine it to salmon's directories.

    Jobs walk, transcode and delete inside the returned path, so we resolve
    symlinks (realpath — a lexical check is defeated by a symlinked component)
    and require the real target to sit within a configured root. This blocks
    '/', ``$HOME`` and bind mounts, which would sweep the whole system.
    """
    path = os.path.realpath(os.path.expanduser(raw_path))
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail=f"Not a directory: {raw_path}")
    if not is_within_roots(path):
        raise HTTPException(
            status_code=403,
            detail="Refusing to operate outside the configured salmon directories.",
        )
    return path
