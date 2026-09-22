"""BaseScraper's sessions refuse a non-public address, including one reached through a redirect."""

import socket

import anyio
import pytest

from salmon.sources.base import _PublicOnlyResolver


class _FakeResolver:
    def __init__(self, *addresses: str):
        self._addresses = addresses
        self.closed = False

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [
            {"hostname": host, "host": a, "port": port, "family": family, "proto": 0, "flags": 0}
            for a in self._addresses
        ]

    async def close(self) -> None:
        self.closed = True


def _resolve(monkeypatch, *addresses: str):
    resolver = _PublicOnlyResolver()
    monkeypatch.setattr(resolver, "_resolver", _FakeResolver(*addresses))
    return anyio.run(resolver.resolve, "example.test")


def test_a_public_address_resolves(monkeypatch) -> None:
    results = _resolve(monkeypatch, "93.184.216.34")
    assert [r["host"] for r in results] == ["93.184.216.34"]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.20.11",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
    ],
)
def test_a_non_public_address_is_refused(address, monkeypatch) -> None:
    with pytest.raises(OSError, match="non-public address"):
        _resolve(monkeypatch, address)


def test_one_bad_address_among_good_ones_refuses_the_whole_host(monkeypatch) -> None:
    # A DNS answer mixing a public and a loopback record must not be usable.
    with pytest.raises(OSError, match="non-public address"):
        _resolve(monkeypatch, "93.184.216.34", "127.0.0.1")
