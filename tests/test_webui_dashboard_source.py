"""The dashboard's front end: what it sends when the page loads (webui/src/pages/Dashboard.svelte)."""

import re
from pathlib import Path

DASHBOARD = Path(__file__).parents[1] / "webui" / "src" / "pages" / "Dashboard.svelte"


def test_the_dashboard_asks_the_trackers_nothing_when_the_page_loads() -> None:
    # As upstream's: the tracker check runs only from its button, never from an effect that runs on load.
    source = DASHBOARD.read_text(encoding="utf-8")
    effects = re.findall(r"\$effect\(\(\) => \{(.*?)\n  \}\)", source, flags=re.DOTALL)

    assert effects, "the dashboard's effects are no longer written as this test expects: update it"
    assert not [effect for effect in effects if "checkconf" in effect]
    assert "onclick={() => checkconf(true)}" in source
