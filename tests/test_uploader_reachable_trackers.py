"""_reachable_trackers: the trackers a run may upload to, and what salmon up passes it."""

from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from asyncclick.testing import CliRunner
from test_uploader_dry_run import (  # pyright: ignore[reportMissingImports]
    _album,
    _loose_rate_limit,  # noqa: F401 (a fixture: see the test using it)
    _run_up,
)

import salmon.trackers
import salmon.uploader as uploader
from salmon import cfg
from salmon.uploader import _reachable_trackers


@pytest.fixture(autouse=True)
def three_sites(monkeypatch):
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])


def test_no_trackers_are_known_up_front_without_a_list():
    """What salmon web passes when no tracker was ticked: each next one is asked for."""
    assert _reachable_trackers(None, offer_others=False) is None


def test_named_trackers_are_the_runs_alone():
    # Ticking OPS and DIC must never lead to RED being offered.
    assert _reachable_trackers(["OPS", "DIC"], offer_others=False) == ["OPS", "DIC"]


@pytest.mark.parametrize(("multi_tracker_upload", "offered"), [(True, ["RED", "OPS", "DIC"]), (False, ["RED", "OPS"])])
def test_trackers_no_t_named_are_followed_by_the_others_if_multi_tracker_upload(
    monkeypatch, multi_tracker_upload: bool, offered: list[str]
):
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", multi_tracker_upload)
    assert _reachable_trackers(["RED", "OPS"], offer_others=True) == offered


async def _up_with(monkeypatch, tmp_path: Path, defaults: list[str], *args: str) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_upload(*_args: Any, **kwargs: Any) -> None:
        seen.append(kwargs)

    monkeypatch.setattr(salmon.trackers.tracker_cfg, "default_trackers", defaults)
    monkeypatch.setattr(uploader, "upload", fake_upload)
    result = await CliRunner().invoke(uploader.up, [str(tmp_path), "-s", "WEB", *args])
    assert result.exit_code == 0, result.output
    return seen


@pytest.mark.parametrize(
    ("defaults", "args", "trackers", "offer_others"),
    [
        (["RED"], ["-t", "RED"], ["RED"], False),
        (["RED"], ["-t", "RED,OPS"], ["RED", "OPS"], False),
        (["RED", "OPS"], [], ["RED", "OPS"], True),
        (["RED"], [], ["RED"], True),
    ],
    ids=["one_named", "several_named", "several_defaults", "one_default"],
)
def test_the_run_uploads_to_the_trackers_named_exactly_and_offers_others_after_unnamed_ones(
    monkeypatch, tmp_path, defaults: list[str], args: list[str], trackers: list[str], offer_others: bool
):
    seen = anyio.run(lambda: _up_with(monkeypatch, tmp_path, defaults, *args))

    assert [(kwargs["trackers"], kwargs["offer_others"]) for kwargs in seen] == [(trackers, offer_others)]


@pytest.mark.usefixtures("_loose_rate_limit")
@pytest.mark.parametrize(("trackers", "limit"), [(("OPS",), 255), (("OPS", "RED"), 180)])
def test_the_run_checks_the_folder_against_the_strictest_limit_of_its_trackers(
    monkeypatch, tmp_path, trackers: tuple[str, ...], limit: int
) -> None:
    # Through the real run, which stops at the review: OPS allows 255 characters, RED 180.
    seen: list[int] = []

    async def edit_metadata(*_args: Any, max_path_length: int, **_kwargs: Any) -> None:
        seen.append(max_path_length)
        raise click.Abort

    downloads, torrents = tmp_path / "downloads", tmp_path / "torrents"
    downloads.mkdir()
    torrents.mkdir()
    monkeypatch.setattr(cfg.directory, "download_directory", str(downloads))
    monkeypatch.setattr(cfg.directory, "tmp_dir", None)
    monkeypatch.setattr(cfg.directory, "library_dirs", [])
    run = _run_up(monkeypatch, _album(tmp_path / "album"), torrents, trackers=trackers, edit_metadata=edit_metadata)

    assert run.result.exit_code == 0, run.result.output
    assert seen == [limit]
