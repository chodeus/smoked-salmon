"""Upstream's rules for which Host and Origin headers ``salmon web`` accepts, as plain functions (security.py)."""

import pytest

from salmon.webui.security import host_allowed, origin_allowed, split_host


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        ("http://127.0.0.1:55155", "127.0.0.1:55155", True),
        ("http://LOCALHOST:55155", "localhost:55155", True),
        ("http://[::1]:55155", "[::1]:55155", True),
        # Behind a TLS reverse proxy that passes Host through.
        ("https://salmon.example", "salmon.example", True),
        ("https://salmon.example", "salmon.example:443", True),
        ("https://salmon.example:8443", "salmon.example:8443", True),
        ("http://salmon.example", "salmon.example:80", True),
        ("https://salmon.example", "salmon.example:80", False),
        ("http://127.0.0.1:55155", "127.0.0.1:55156", False),
        ("http://127.0.0.1", "127.0.0.1:55155", False),
        ("http://evil.example:55155", "127.0.0.1:55155", False),
        ("http://127.0.0.1.evil.example:55155", "127.0.0.1:55155", False),
        ("null", "127.0.0.1:55155", False),
        ("file://", "127.0.0.1:55155", False),
        ("http://127.0.0.1:99999", "127.0.0.1:55155", False),
        ("http://127.0.0.1:55155", None, False),
    ],
)
def test_origin_rule(origin: str, host: str | None, allowed: bool) -> None:
    assert origin_allowed(origin, host) is allowed


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "::"])  # nosec B104 # bind addresses under test: nothing is served
def test_a_wildcard_bind_is_no_host_name(bind_host: str) -> None:
    assert not host_allowed("0.0.0.0:55155", bind_host, [])
    assert not host_allowed("[::]:55155", bind_host, [])
    assert host_allowed("localhost:55155", bind_host, [])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("example.org", ("example.org", None)),
        ("Example.org:80", ("example.org", 80)),
        ("[::1]:55155", ("::1", 55155)),
        ("[::1]", ("::1", None)),
    ],
)
def test_split_host(value: str, expected: tuple[str, int | None]) -> None:
    assert split_host(value) == expected


@pytest.mark.parametrize("value", ["", ":80", "[::1", "[::1]x", "host:", "host:http", "host:0", "host:70000"])
def test_split_host_refuses_garbage(value: str) -> None:
    with pytest.raises(ValueError):
        split_host(value)
