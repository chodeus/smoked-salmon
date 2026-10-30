"""What this fork's egress filter adds to upstream's: each tracker's keeplogged cookie, a setting only the fork has."""

import pytest

from salmon import cfg
from salmon.config.validations import GazelleTrackerSettings
from salmon.webui.egress import Redactor, config_secrets

KEEPLOGGED = "planted-keeplogged-cookie-71ac0d"


@pytest.mark.parametrize("tracker", ["red", "ops", "dic"])
def test_the_filter_masks_a_trackers_keeplogged_cookie_on_its_own(
    monkeypatch: pytest.MonkeyPatch, tracker: str
) -> None:
    settings = GazelleTrackerSettings(session="planted-session-cookie-5e2b", keeplogged=KEEPLOGGED)
    monkeypatch.setattr(cfg.tracker, tracker, settings)

    masked = Redactor(config_secrets(cfg)).text(f"the cookie was {KEEPLOGGED}, alone")

    assert KEEPLOGGED not in masked
    assert masked.startswith("the cookie was ")
