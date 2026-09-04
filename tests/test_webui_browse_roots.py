"""The picker can only reach the root it opened at unless /browse names them all."""

import os

import pytest
from fastapi.testclient import TestClient

from salmon import cfg
from salmon.webui.app import create_app


@pytest.fixture
def client():
    with TestClient(create_app(), base_url="http://localhost") as c:
        yield c


def test_browse_lists_every_configured_root(client, tmp_path, monkeypatch) -> None:
    lib = tmp_path / "music"
    (lib / "Artist").mkdir(parents=True)
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    body = client.get("/api/browse").json()
    paths = {r["path"] for r in body["roots"]}
    assert str(lib.resolve()) in paths, "the library must be reachable from the default view"
    assert len(paths) > 1


def test_a_library_root_is_flagged_as_such(client, tmp_path, monkeypatch) -> None:
    lib = tmp_path / "music"
    lib.mkdir()
    monkeypatch.setattr(cfg.directory, "library_dirs", [str(lib)])

    roots = {r["path"]: r for r in client.get("/api/browse").json()["roots"]}
    assert roots[str(lib.resolve())]["library"] is True
    other = [r for p, r in roots.items() if p != str(lib.resolve())]
    assert other and all(r["library"] is False for r in other)


def test_the_default_view_is_still_the_download_directory(client) -> None:
    body = client.get("/api/browse").json()
    assert body["path"] == os.path.realpath(cfg.directory.download_directory)


@pytest.mark.parametrize("where", ["/", "/etc"])
def test_browsing_outside_the_roots_is_still_refused(client, where) -> None:
    assert client.get(f"/api/browse?path={where}").status_code == 403


def test_an_outside_path_is_refused_before_it_is_probed(client) -> None:
    """A 404 for a missing path outside the roots would say the path does not exist."""
    response = client.get("/api/browse?path=/nonexistent-salmon-zzz")
    assert response.status_code == 403
