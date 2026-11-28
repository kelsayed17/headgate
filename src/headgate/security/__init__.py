# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Where a client's requests and credentials may go: credential origin pinning, redirect hardening, the SSRF guard.

``Client`` installs all of it from :class:`~headgate.SecurityOptions`; nothing here needs calling directly.
Credentials are sent only to ``base_url``'s origin and ``SecurityOptions.trusted_origins``, on every hop, and a
redirect from ``https`` to ``http`` is not followed. The SSRF guard (``SecurityOptions(block_private_hosts=True)``)
refuses hosts that are, or resolve to, addresses that are not globally routable, raising
:class:`~headgate.errors.ForbiddenDestinationError`.

:class:`CredentialCarrier` and :class:`CredentialStamper` declare the headers and query parameters a seam stamps, so
the guard keeps them on trusted origins and the cache partitions by them: a middleware implements the first, or the
second when it has a credential identity of its own, and every :class:`~headgate.Auth` is a ``CredentialStamper`` by
its shape.

What else is public here is what the guard is configured with, in ``SecurityOptions``: a :data:`Resolver`
(``resolver=``) and an :data:`AddressCheck` (``check=``), of which :func:`forbidden_reason` is the default, so a check
of your own can defer to it (a test reaching a loopback server admits that one address).

Importing headgate also redacts the URL in httpx2's own ``HTTP Request:`` log line.
"""

from headgate._core import CredentialCarrier, CredentialStamper
from headgate._core import rehome as _rehome
from headgate.security._addresses import AddressCheck, Resolver, forbidden_reason
from headgate.security._logging import redact_httpx2_log as _redact_httpx2_log

# httpx2 logs each request's URL as sent, credentials and all (see headgate.security._logging).
_redact_httpx2_log()

__all__ = [
    "AddressCheck",
    "CredentialCarrier",
    "CredentialStamper",
    "Resolver",
    "forbidden_reason",
]

_rehome(__name__, globals())
