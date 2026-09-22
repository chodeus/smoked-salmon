"""http_url_hostname: the one place deciding whether a string is a usable http(s) URL."""

import pytest

from salmon.common import http_url_hostname, is_http_url


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("https://files.catbox.moe/abc.jpg", "files.catbox.moe"),
        ("http://example.com:8080/x", "example.com"),
        ("https://user:pw@example.com/x", "example.com"),
        ("http://[::1]/", "::1"),
    ],
)
def test_a_usable_url_yields_its_hostname(value, host) -> None:
    assert http_url_hostname(value) == host


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "https://",
        "https:///abc.jpg",
        "//example.com/x",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "Something went wrong, please try again later.",
        # yarl raises on these; a parse failure must read as "no hostname", never propagate.
        "http://[::1",
        "https://example.com:notaport/x",
        # yarl raises TypeError, not ValueError, for anything that is not a str - and a host's
        # JSON url field can be a number.
        123,
        4.5,
        b"https://files.catbox.moe/a.jpg",
        ["https://files.catbox.moe/a.jpg"],
    ],
)
def test_an_unusable_value_has_no_hostname(value) -> None:
    assert http_url_hostname(value) is None


def test_is_http_url_tracks_the_hostname_lookup() -> None:
    usable = is_http_url("https://example.com/x")
    unusable = is_http_url("https://")
    assert usable is True
    assert unusable is False
