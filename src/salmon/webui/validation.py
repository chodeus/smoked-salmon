"""Shared request validation for webui routers."""

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlparse

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
    for root in roots:
        # commonpath, not startswith: "root + os.sep" is "//" when root is "/".
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except ValueError:  # different drives on Windows
            continue
    return False


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


def refuse_library_output(output_path: str, what: str) -> None:
    """Refuse a job whose OUTPUT would land inside a read-only library source.

    For jobs that write beside the album rather than into it, the album being a
    library source says nothing — where the output lands is what matters.
    """
    if cfg.directory.is_library_path(output_path):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{what} would be written inside a read-only library directory. "
                "Set directory.tmp_dir to a writable scratch folder."
            ),
        )


_BLOCKED_IP_ATTRS = ("is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(getattr(ip, attr) for attr in _BLOCKED_IP_ATTRS)


async def assert_public_url(url: str) -> None:
    """Reject non-http(s) URLs and any host resolving to a private/internal address.

    Bandcamp uses per-artist custom domains, so scrapers match arbitrary hosts;
    without this, /api/metadata?url=http://<internal-host>/album/x is an SSRF
    that makes the server fetch internal services.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=422, detail="Only http(s) URLs are supported.")
    host = parsed.hostname
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        except socket.gaierror as e:
            raise HTTPException(status_code=422, detail=f"Could not resolve host: {host}") from e
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]
    if any(_is_blocked_ip(addr) for addr in addrs):
        raise HTTPException(status_code=422, detail="Refusing to fetch a non-public address.")
