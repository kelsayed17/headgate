# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Which addresses the SSRF guard refuses, and how :class:`AddressGuard` resolves and vets a host."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

import pytest

from headgate import ForbiddenDestinationError
from headgate.security import AddressGuard, forbidden_reason, resolve_with_loop

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        (str(ipaddress.IPv4Address(0)), "unspecified"),
        ("::", "unspecified"),
        ("127.0.0.1", "loopback"),
        ("127.255.0.9", "loopback"),
        ("::1", "loopback"),
        ("169.254.169.254", "link-local"),
        ("fe80::1%en0", "link-local"),
        ("224.0.0.1", "multicast"),
        ("224.0.1.1", "multicast"),
        ("ff0e::1", "multicast"),
        ("10.0.0.1", "private"),
        ("172.16.0.1", "private"),
        ("192.168.1.1", "private"),
        ("fd00::1", "private"),
        ("240.0.0.1", "private"),
        ("2001:db8::1", "private"),
        ("2002:7f00:1::", "private"),
        ("::7f00:1", "reserved"),
        ("100.64.0.1", "non-global"),
        ("::ffff:127.0.0.1", "loopback"),
        ("::ffff:10.0.0.1", "private"),
        ("64:ff9b::7f00:1", "loopback"),
        ("64:ff9b::a9fe:a9fe", "link-local"),
    ],
)
def test_a_non_global_address_is_refused_with_its_reason(address: str, reason: str) -> None:
    """Name why an address that is not globally routable is refused, judging embedded IPv4 by what it reaches."""
    assert forbidden_reason(address) == reason


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"])
def test_a_global_address_is_allowed(address: str) -> None:
    """Allow a public address, an IPv4-mapped or NAT64 form of one included."""
    assert forbidden_reason(address) is None


def test_a_name_is_not_an_address() -> None:
    """Raise ``ValueError`` for text that is not an IP address."""
    with pytest.raises(ValueError, match="does not appear to be an IPv4 or IPv6 address"):
        forbidden_reason("example.com")


async def test_the_default_resolver_asks_the_event_loop() -> None:
    """Resolve through the running loop's ``getaddrinfo``, each address once."""
    addresses = await resolve_with_loop("localhost", 80)

    assert addresses
    assert len(addresses) == len(set(addresses))
    assert all(forbidden_reason(address) == "loopback" for address in addresses)


class _Answer:
    """A resolver answering every name with *addresses*, recording each lookup."""

    def __init__(self, *addresses: str) -> None:
        """Answer with *addresses*."""
        self._addresses = addresses
        self.lookups: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> Sequence[str]:
        """Record the lookup and answer it."""
        self.lookups.append((host, port))
        return self._addresses


class TestAddressGuard:
    """``AddressGuard.vet``: every address a host resolves to must pass."""

    async def test_a_public_name_returns_its_addresses_in_order(self) -> None:
        """Return the resolver's addresses, in its order, when all are allowed."""
        resolver = _Answer("8.8.8.8", "2606:4700::1111")

        assert await AddressGuard(resolver).vet("api.example", 443) == ("8.8.8.8", "2606:4700::1111")
        assert resolver.lookups == [("api.example", 443)]

    @pytest.mark.parametrize("literal", ["127.0.0.1", "::1"])
    async def test_an_ip_literal_is_checked_without_a_lookup(self, literal: str) -> None:
        """Judge an IP literal as it is, never asking the resolver."""
        resolver = _Answer("8.8.8.8")

        with pytest.raises(ForbiddenDestinationError, match=f"refused to connect to {literal}: it is a loopback"):
            await AddressGuard(resolver).vet(literal, 80)
        assert resolver.lookups == []

    async def test_one_forbidden_address_refuses_the_name(self) -> None:
        """Refuse a name with any forbidden address among its answers, naming it and the reason."""
        guard = AddressGuard(_Answer("8.8.8.8", "169.254.169.254"))

        with pytest.raises(ForbiddenDestinationError, match=r"resolves to 169\.254\.169\.254, which is a link-local"):
            await guard.vet("metadata.example", 80)

    async def test_an_empty_answer_is_a_lookup_failure(self) -> None:
        """Raise ``OSError`` for a name that resolves to nothing, as a failed lookup does."""
        with pytest.raises(OSError, match="resolved to no address"):
            await AddressGuard(_Answer()).vet("void.example", 80)

    async def test_the_check_is_injectable(self) -> None:
        """Judge addresses with the check given, so a test can admit a loopback server."""
        guard = AddressGuard(_Answer("127.0.0.1"), check=lambda _address: None)

        assert await guard.vet("api.example", 80) == ("127.0.0.1",)
