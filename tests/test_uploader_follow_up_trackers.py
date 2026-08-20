"""follow_up_trackers: which sites the continuation prompt may offer."""

import pytest

import salmon.trackers
from salmon.uploader import follow_up_trackers


@pytest.fixture(autouse=True)
def three_sites(monkeypatch):
    monkeypatch.setattr(salmon.trackers, "tracker_list", ["RED", "OPS", "DIC"])


def test_none_offers_every_configured_site():
    """What the CLI passes — behaviour must be unchanged from before the allow-list."""
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
