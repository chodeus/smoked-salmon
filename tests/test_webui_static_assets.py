"""Every icon index.html asks for must actually ship, or the tab falls back to blank."""

import re
from pathlib import Path

WEBUI = Path(__file__).resolve().parent.parent / "webui"


def test_referenced_icons_exist_in_public() -> None:
    index = (WEBUI / "index.html").read_text()
    refs = re.findall(r'<link[^>]+rel="(?:icon|apple-touch-icon)"[^>]+href="/([^"]+)"', index)
    assert refs, "index.html declares no icon at all"
    missing = [r for r in refs if not (WEBUI / "public" / r).is_file()]
    assert not missing, f"index.html references icons that are not in public/: {missing}"


def test_the_icon_is_the_project_logo_not_an_emoji() -> None:
    """It shipped as an SVG holding a text emoji, which is why the tab showed a
    generic fish rather than the salmon."""
    index = (WEBUI / "index.html").read_text()
    assert "favicon.svg" not in index
    assert not list((WEBUI / "public").glob("*.svg")), "an emoji-in-SVG favicon is back"
