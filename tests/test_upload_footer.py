"""The description generators must carry the same attribution footer."""

import importlib

import pytest

from salmon.converter.downconverting import generate_conversion_description
from salmon.converter.transcoding import generate_transcode_description
from salmon.release_notification import FORK_URL, UPSTREAM_URL, upload_footer


@pytest.fixture(autouse=True)
def pinned_version(monkeypatch):
    monkeypatch.setattr(importlib.import_module("salmon.release_notification"), "get_version", lambda: "1.0.0-test")


def test_footer_names_the_fork_and_credits_upstream():
    footer = upload_footer()
    assert f"[url={FORK_URL}]" in footer
    assert f"[url={UPSTREAM_URL}]" in footer
    assert "v1.0.0-test (chodeus fork)" in footer


@pytest.mark.parametrize(
    "description",
    [
        lambda: generate_conversion_description("https://example.com/a", 44100, 16),
        lambda: generate_transcode_description("https://example.com/a", "320"),
    ],
    ids=["downconvert", "transcode"],
)
def test_converter_descriptions_end_with_the_shared_footer(description):
    assert description().endswith(upload_footer())


@pytest.mark.parametrize(
    ("description", "already_has_one"),
    [
        ("notes\n{current}", True),
        (f"notes\n[hr]Uploaded with [url={FORK_URL}][b]smoked-salmon[/b] v0.10.1 (chodeus fork)[/url]", True),
        (f"notes\n[hr]Uploaded with [url={UPSTREAM_URL}][b]smoked-salmon[/b] v0.10.1[/url]", True),
        (f"See {UPSTREAM_URL} for the tool used.", False),
        ("Uploaded with [url=https://example.invalid/smoked-salmon]another tool[/url]", False),
        (f"Uploaded with [url={FORK_URL}]another tool[/url]", False),
        ("notes\n{current}\n\nmore notes after it", True),
        ("a description from another tool", False),
    ],
    ids=[
        "current-version",
        "older-version",
        "upstream-tool",
        "bare-link-in-prose",
        "lookalike-host",
        "right-host-wrong-shape",
        "footer-not-last",
        "none",
    ],
)
def test_footer_detection_is_version_and_fork_agnostic(description, already_has_one):
    from salmon.release_notification import has_upload_footer

    assert has_upload_footer(description.format(current=upload_footer())) is already_has_one
