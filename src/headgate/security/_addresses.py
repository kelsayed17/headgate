# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Which network addresses a guarded client may connect to, and the resolver that finds them.

An address is allowed only when it is globally routable (:attr:`ipaddress.IPv4Address.is_global`) and neither multicast
nor reserved. An address that carries an IPv4 address inside an IPv6 one is judged by the IPv4 address it reaches:
IPv4-mapped (``::ffff:127.0.0.1``) and the NAT64 well-known prefix (``64:ff9b::7f00:1``), so neither smuggles a
loopback or private target past the check, and an IPv6-only network with DNS64 still reaches public IPv4 hosts. Other
transition forms (6to4, Teredo, IPv4-compatible) are not globally routable or are reserved, so they are refused
outright.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import TYPE_CHECKING, Final

from headgate.errors import ForbiddenDestinationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

__all__ = ["AddressCheck", "AddressGuard", "Resolver", "forbidden_reason", "resolve_with_loop"]

#: Resolves a host name and port to the IP addresses (as text) a connection may be made to, in preference order.
type Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

#: Returns why an address (as text) may not be connected to, or ``None`` when it may.
type AddressCheck = Callable[[str], str | None]

type _Address = ipaddress.IPv4Address | ipaddress.IPv6Address

#: The NAT64 well-known prefix (RFC 6052): the low 32 bits are the IPv4 destination the translator reaches.
_NAT64: Final = ipaddress.IPv6Network("64:ff9b::/96")

#: The low 32 bits of an IPv6 address, where NAT64 carries the IPv4 address.
_LOW_32_BITS: Final = 0xFFFF_FFFF


async def resolve_with_loop(host: str, port: int) -> list[str]:
    """Resolve *host* through the running event loop's ``getaddrinfo``, keeping the order and dropping repeats."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _reached(address: _Address) -> _Address:
    """Return the address a connection to *address* actually reaches: the IPv4 one it embeds, or itself."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in _NAT64:
            return ipaddress.IPv4Address(int(address) & _LOW_32_BITS)
    return address


def forbidden_reason(address: str) -> str | None:
    """Return why the guard refuses *address* (``"loopback"``, ``"private"``, ...), or ``None`` when it is allowed.

    *address* is an IPv4 or IPv6 address in text form; an IPv6 zone (``fe80::1%en0``) is accepted.

    :raises ValueError: when *address* is not an IP address.
    """
    ip = _reached(ipaddress.ip_address(address))
    checks = (
        (ip.is_unspecified, "unspecified"),
        (ip.is_loopback, "loopback"),
        (ip.is_link_local, "link-local"),
        (ip.is_multicast, "multicast"),
        (ip.is_private, "private"),
        (ip.is_reserved, "reserved"),
        (not ip.is_global, "non-global"),
    )
    return next((reason for refused, reason in checks if refused), None)


def _ip_literal(host: str) -> str | None:
    """Return *host* when it is an IP address rather than a name, else ``None``."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


class AddressGuard:
    """Resolve a host and refuse it unless every address it resolves to passes the check.

    Every address must pass, not just the first: a name that answers with one public and one private address could
    otherwise be connected to the private one by any client that falls back along the list. An IP literal is checked
    as it is, without a lookup.
    """

    __slots__ = ("_check", "_resolver")

    def __init__(self, resolver: Resolver = resolve_with_loop, *, check: AddressCheck = forbidden_reason) -> None:
        """Resolve names with *resolver* and judge addresses with *check* (tests widen it to reach loopback servers)."""
        self._resolver = resolver
        self._check = check

    async def vet(self, host: str, port: int) -> tuple[str, ...]:
        """Return the addresses *host* may be connected to on *port*, in the resolver's order.

        :raises ForbiddenDestinationError: when any address is refused.
        :raises OSError: when the name does not resolve (``socket.gaierror``), or resolves to nothing.
        """
        literal = _ip_literal(host)
        addresses = (literal,) if literal is not None else tuple(await self._resolver(host, port))
        if not addresses:
            msg = f"{host} resolved to no address"
            raise OSError(msg)
        for address in addresses:
            reason = self._check(address)
            if reason is not None:
                where = "is" if literal is not None else f"resolves to {address}, which is"
                msg = f"refused to connect to {host}: it {where} a {reason} address"
                raise ForbiddenDestinationError(msg)
        return addresses
