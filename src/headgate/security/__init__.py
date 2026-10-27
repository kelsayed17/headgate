# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Where a client's requests and credentials may go: credential origin pinning, redirect hardening, the SSRF guard.

``AsyncBaseClient`` installs all of it from :class:`~headgate.SecurityOptions`; nothing here needs calling directly.
Credentials are sent only to ``base_url``'s origin and ``SecurityOptions.trusted_origins``, on every hop, and a
redirect from ``https`` to ``http`` is not followed. The SSRF guard (``SecurityOptions(allow_private_hosts=False)``)
refuses hosts that are, or resolve to, addresses that are not globally routable, raising
:class:`~headgate.errors.ForbiddenDestinationError`.

What is public here plugs into the guard: a :data:`Resolver` (``HttpOptions(resolver=...)``), and
:class:`AddressGuard` and :class:`PinnedHTTPTransport`, so that a test can reach a loopback server through the real
pinning transport (``AddressGuard(check=...)``). :func:`forbidden_reason` is the default :data:`AddressCheck`, and
:func:`resolve_with_loop` the default resolver.
"""

from headgate.security._addresses import AddressCheck, AddressGuard, Resolver, forbidden_reason, resolve_with_loop
from headgate.security._transport import PinnedHTTPTransport

__all__ = [
    "AddressCheck",
    "AddressGuard",
    "PinnedHTTPTransport",
    "Resolver",
    "forbidden_reason",
    "resolve_with_loop",
]
