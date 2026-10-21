"""follow_up_trackers: which sites the continuation prompt may offer."""

from pathlib import Path
from typing import Any

import anyio
import pytest
from asyncclick.testing import CliRunner

import salmon.trackers
import salmon.uploader as uploader
from salmon import cfg
from salmon.uploader import follow_up_trackers, next_tracker


@pytest.fixture(autouse=True)
def three_sites(monkeypatch):
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])


def test_none_offers_every_configured_site():
    """What salmon web passes when no other tracker was picked."""
    assert follow_up_trackers(None, "RED") == ["RED", "OPS", "DIC"]


def test_a_selection_excludes_the_sites_not_chosen():
    # picking OPS and DIC must never lead to RED being offered
    assert follow_up_trackers(["OPS", "DIC"], "OPS") == ["OPS", "DIC"]


def test_repeats_are_collapsed_so_a_site_cannot_be_offered_twice():
    """One entry is removed per upload, so a duplicate would be re-offered and
    could produce a second upload to the same tracker."""
    assert follow_up_trackers(["RED", "OPS", "RED"], "RED") == ["RED", "OPS"]


def test_caller_order_is_preserved():
    assert follow_up_trackers(["DIC", "RED", "OPS"], "DIC") == ["DIC", "RED", "OPS"]


def test_the_current_site_is_always_present_for_its_own_removal():
    # upload() calls .remove(tracker) after uploading; a missing entry would raise
    assert follow_up_trackers(["OPS"], "RED") == ["RED", "OPS"]


def test_an_empty_selection_offers_nothing_further():
    assert follow_up_trackers([], "RED") == ["RED"]


@pytest.mark.parametrize(("multi_tracker_upload", "offered"), [(True, ["RED", "OPS", "DIC"]), (False, ["RED", "OPS"])])
def test_trackers_no_t_named_are_followed_by_the_others_if_multi_tracker_upload(
    monkeypatch, multi_tracker_upload: bool, offered: list[str]
):
    monkeypatch.setattr(cfg.upload, "multi_tracker_upload", multi_tracker_upload)
    assert follow_up_trackers(["RED", "OPS"], "RED", offer_others=True) == offered


def test_the_runs_own_trackers_go_on_unasked_and_the_others_are_asked_about(monkeypatch):
    asked: list[list[str]] = []

    async def choose(choices: list[str]) -> str | None:
        asked.append(list(choices))
        return None

    monkeypatch.setattr(salmon.trackers, "choose_tracker", choose)

    async def next_two() -> list[str | None]:
        return [await next_tracker(["RED", "OPS"], ["OPS", "DIC"]), await next_tracker(["RED", "OPS"], ["DIC"])]

    assert anyio.run(next_two) == ["OPS", None]
    assert asked == [["DIC"]]


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
