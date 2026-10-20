"""The Spectrals page sends one job's POST at a time: a second click before the answer would queue a second job."""

import pathlib
import re

PAGE = pathlib.Path(__file__).resolve().parent.parent / "webui" / "src" / "pages" / "Spectrals.svelte"


def _function(source: str, name: str) -> str:
    found = re.search(rf"\n  async function {name}\(\) \{{\n(.*?)\n  \}}\n", source, re.DOTALL)
    assert found is not None, name
    return found.group(1)


def test_each_job_button_is_off_while_its_post_is_in_flight():
    source = PAGE.read_text(encoding="utf-8")
    for name in ("generate", "upload"):
        body = _function(source, name)
        assert re.search(r"if \([^)]*\bstarting\) return\n\s*starting = true\n", body), name
        assert re.search(r"\} finally \{\n\s*starting = false\n\s*\}$", body), name
        assert re.search(rf"onclick=\{{{name}\}} disabled=\{{[^}}]*\bstarting\b", source), name
