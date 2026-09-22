"""_cover_host_for_new_group: whether a tracker reuses a cached RED cover or uploads its own."""

from salmon import cfg
from salmon.uploader import _cover_host_for_new_group


def test_reuses_the_cached_red_cover_for_a_proxy_target() -> None:
    # OPS proxies and caches RED-hosted images, so a re-upload would be redundant.
    stored = {"red": "https://redacted.sh/i/x.jpg"}
    assert _cover_host_for_new_group("OPS", stored) == "red"


def test_falls_back_to_the_tracker_default_without_a_cached_red_cover() -> None:
    assert _cover_host_for_new_group("OPS", {}) == cfg.image.resolve("OPS", "cover_uploader")


def test_a_non_proxy_target_never_reuses_reds_cover() -> None:
    # DIC has no proxy arrangement with RED, so it still needs its own upload.
    stored = {"red": "https://redacted.sh/i/x.jpg"}
    assert _cover_host_for_new_group("DIC", stored) == cfg.image.resolve("DIC", "cover_uploader")


def test_red_follows_its_configured_host() -> None:
    # No [image.red] section in the test config, so RED uses the global cover host.
    host = _cover_host_for_new_group("RED", {})
    assert host == cfg.image.resolve("RED", "cover_uploader")
