# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Where a client's requests and credentials may go: credential origin pinning, redirect hardening, the SSRF guard.

``AsyncBaseClient`` installs all of it from :class:`~headgate.HttpOptions`; nothing here needs calling directly.

* :class:`CredentialGuard` (a request hook, always on): credentials are sent only to ``base_url``'s origin and
  ``HttpOptions.trusted_origins``, on every hop.
* :func:`refuse_https_downgrade` (a response hook, always on): a redirect from ``https`` to ``http`` is not followed.
* The SSRF guard (``HttpOptions(allow_private_hosts=False)``): :class:`AddressGuard` refuses hosts that are, or resolve
  to, addresses that are not globally routable; :class:`PinnedHTTPTransport` pins each connection to the address it
  vetted, and :func:`address_hook` vets every hop for a custom transport. A refusal raises
  :class:`~headgate.errors.ForbiddenDestinationError`.

:class:`AddressGuard` and :class:`PinnedHTTPTransport` are public so that a test can reach a loopback server through
the real pinning transport (``AddressGuard(check=...)``), and so a deployment can plug in its own :data:`Resolver`.
"""

from headgate.security._addresses import AddressCheck, AddressGuard, Resolver, forbidden_reason, resolve_with_loop
from headgate.security._credentials import CHAIN_ORIGIN, CredentialGuard
from headgate.security._downgrade import refuse_https_downgrade
from headgate.security._transport import PinnedHTTPTransport, address_hook

__all__ = [
    "CHAIN_ORIGIN",
    "AddressCheck",
    "AddressGuard",
    "CredentialGuard",
    "PinnedHTTPTransport",
    "Resolver",
    "address_hook",
    "forbidden_reason",
    "refuse_https_downgrade",
    "resolve_with_loop",
]
