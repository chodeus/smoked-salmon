"""The description generators must carry the same attribution footer."""

import importlib

import pytest

from salmon.converter.downconverting import generate_conversion_description
from salmon.converter.transcoding import generate_transcode_description
from salmon.release_notification import FORK_URL, UPSTREAM_URL, upload_footer


@pytest.fixture(autouse=True)
def pinned_version(monkeypatch):
    monkeypatch.setattr(
        importlib.import_module("salmon.release_notification"), "get_version", lambda: "1.0.0-test"
    )


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
