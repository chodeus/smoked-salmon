from typing import Any, cast

import salmon.trackers.red as red_mod
from salmon.trackers.base import BaseGazelleApi


class _Files:
    log_files: list = []
    torrent_data = b"x"


def _files() -> Any:
    return cast("Any", _Files())


async def test_base_dry_run_upload_returns_zero_and_no_network():
    api = BaseGazelleApi.__new__(BaseGazelleApi)
    api.site_string = "OPS"
    assert await api.dry_run_upload({"title": "x"}, _files()) == (0, 0)


async def test_red_dry_run_never_contacts_the_tracker(monkeypatch):
    """RED must not override dry_run_upload with a server-side dryrun.

    That dryrun POSTed the entire upload form to RED. Validating by posting is
    still posting, and --dry-run promises the opposite.
    """
    api = red_mod.RedApi.__new__(red_mod.RedApi)
    api.api_key = "k"
    api.authkey = "auth"
    api.base_url = "https://redacted.sh"
    api.site_string = "RED"

    called = {"request": False, "auth": False}

    async def fake_request(*_args, **_kwargs):
        called["request"] = True
        raise AssertionError("a dry run must not send a request to the tracker")

    async def fake_ensure():
        called["auth"] = True

    monkeypatch.setattr(api, "_request", fake_request)
    monkeypatch.setattr(api, "ensure_authenticated", fake_ensure)

    assert await api.dry_run_upload({"title": "x"}, _files()) == (0, 0)
    assert called["request"] is False
    assert called["auth"] is False


def test_red_does_not_define_its_own_dry_run():
    """Inherited, not overridden — the guard against the override coming back."""
    assert "dry_run_upload" not in vars(red_mod.RedApi)
    assert red_mod.RedApi.dry_run_upload is BaseGazelleApi.dry_run_upload
