"""Path confinement for webui filesystem operations (realpath + allowlist)."""

import os

import pytest
from fastapi import HTTPException

from salmon import cfg
from salmon.webui.validation import validate_album_dir


def test_folder_within_download_dir_is_allowed(tmp_path):
    base = os.path.realpath(cfg.directory.download_directory)
    album = os.path.join(base, tmp_path.name, "album")
    os.makedirs(album, exist_ok=True)
    assert validate_album_dir(album) == album


def test_folder_outside_roots_is_rejected():
    # a real system dir that is not one of salmon's configured roots
    with pytest.raises(HTTPException) as excinfo:
        validate_album_dir("/tmp")  # noqa: S108 - deliberately outside the roots
    assert excinfo.value.status_code == 403


def test_symlink_escaping_a_root_is_rejected(tmp_path):
    base = os.path.realpath(cfg.directory.download_directory)
    outside = tmp_path / "secret"
    outside.mkdir()
    link = os.path.join(base, f"sneaky-{tmp_path.name}")
    os.symlink(str(outside), link)
    # lexically 'link' is under a root, but its real target is not
    with pytest.raises(HTTPException) as excinfo:
        validate_album_dir(link)
    assert excinfo.value.status_code == 403


def test_metadata_endpoint_rejects_internal_and_bad_scheme():
    from fastapi.testclient import TestClient

    from salmon.webui.app import create_app

    with TestClient(create_app(), base_url="http://localhost") as c:
        for bad in (
            "http://127.0.0.1/album/x",
            "http://10.0.20.10:8006/album/x",
            "http://[::1]/album/x",
            "file:///etc/passwd",
        ):
            resp = c.get("/api/metadata", params={"url": bad})
            assert resp.status_code == 422, f"{bad} should be rejected, got {resp.status_code}"
