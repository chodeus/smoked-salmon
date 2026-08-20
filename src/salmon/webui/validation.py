"""Shared request validation for webui routers."""

import os

from fastapi import HTTPException

from salmon import cfg


def allowed_roots() -> list[str]:
    """Real paths the UI may operate within: salmon's configured directories.

    library_dirs are sources, so they are browsable and uploadable; the delete
    path refuses them separately (see Directory.is_library_path).
    """
    raw = [
        cfg.directory.download_directory,
        cfg.directory.dottorrents_dir,
        cfg.directory.tmp_dir,
        *cfg.directory.library_dirs,
    ]
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
    roots = allowed_roots()
    # Confinement BEFORE any filesystem probe: an isdir() check on an out-of-root path would
    # leak whether that path exists (CWE-203). Also reject the roots themselves — a job that
    # deletes its path (AbortAndDeleteFolder -> rmtree) must never target a configured root.
    if not is_within_roots(path, roots) or path in roots:
        raise HTTPException(
            status_code=403,
            detail="Refusing to operate outside the configured salmon directories.",
        )
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail=f"Not a directory: {raw_path}")
    return path


def validate_writable_album_dir(raw_path: str) -> str:
    """Like validate_album_dir, but refuses read-only library sources.

    Transcode/downconvert write a sibling folder next to the source and
    spectral generation writes into the album itself, so neither may target a
    curated library_dirs entry.
    """
    path = validate_album_dir(raw_path)
    if cfg.directory.is_library_path(path):
        raise HTTPException(
            status_code=403,
            detail="Refusing to write inside a read-only library directory.",
        )
    return path
