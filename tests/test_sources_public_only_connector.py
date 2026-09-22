"""BaseScraper's sessions refuse a non-public address, as an IP literal or via DNS."""

import socket
from typing import Any, cast

import anyio
import pytest

from salmon.sources.base import _public_only_session, _PublicOnlyConnector


class _FakeResolver:
    def __init__(self, *addresses: str):
        self._addresses = addresses

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [
            {"hostname": host, "host": a, "port": port, "family": family, "proto": 0, "flags": 0}
            for a in self._addresses
        ]

    async def close(self) -> None:
        return None


async def _resolve(host: str, *dns_answers: str):
    connector = _PublicOnlyConnector()
    if dns_answers:
        connector._resolver = cast("Any", _FakeResolver(*dns_answers))
    try:
        return await connector._resolve_host(host, 80)
    finally:
        await connector.close()


@pytest.mark.parametrize(
    "literal",
    [
        "127.0.0.1",
        "::1",
        "10.0.20.11",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",  # CGNAT, where a Tailscale node lives
        "100.127.255.254",
        "198.18.0.1",
        "2001:db8::1",
    ],
)
def test_an_ip_literal_on_a_private_address_is_refused(literal) -> None:
    with pytest.raises(OSError, match="non-public address"):
        anyio.run(_resolve, literal)


def test_a_public_ip_literal_connects() -> None:
    results = anyio.run(_resolve, "93.184.216.34")
    assert [r["host"] for r in results] == ["93.184.216.34"]


def test_a_hostname_resolving_to_a_private_address_is_refused() -> None:
    with pytest.raises(OSError, match="non-public address"):
        anyio.run(_resolve, "rebind.test", "127.0.0.1")


def test_a_hostname_resolving_to_a_public_address_connects() -> None:
    results = anyio.run(_resolve, "example.test", "93.184.216.34")
    assert [r["host"] for r in results] == ["93.184.216.34"]


def test_one_private_answer_among_public_ones_refuses_the_host() -> None:
    with pytest.raises(OSError, match="non-public address"):
        anyio.run(_resolve, "mixed.test", "93.184.216.34", "127.0.0.1")


def test_the_scraper_session_uses_that_connector() -> None:
    async def _build():
        import aiohttp

        async with _public_only_session(aiohttp.ClientTimeout(total=1)) as session:
            return type(session.connector)

    connector_type = anyio.run(_build)
    assert connector_type is _PublicOnlyConnector
