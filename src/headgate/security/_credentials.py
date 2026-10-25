# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Credentials stay on trusted origins: one request hook, run before every hop.

A credential may be sent only to ``base_url``'s origin and the origins in ``HttpOptions.trusted_origins``. httpx2 runs
request hooks after the auth flow has stamped the request and before every hop it sends, so :class:`CredentialGuard`
sees the first request (an absolute cursor URL included), every redirect hop and every hop an auth flow yields. On any
of them addressed elsewhere it removes:

* every header and query parameter the client's auth declares it stamps (a
  :class:`~headgate._core.CredentialStamper`: every auth in headgate);
* ``Authorization`` and ``Proxy-Authorization``, whatever the auth, so a third-party auth or a caller's own header does
  not reach another origin either. httpx2 itself drops ``Authorization`` only on a cross-origin redirect, not on a
  first request to an absolute URL;
* ``Cookie``, unless the client keeps a cookie jar (``HttpOptions(cookies=True)``), which scopes cookies to their
  domains itself.

The request is still sent, unsigned: a provider may hand out a signed download URL on another host. A header of one of
those names that the call set itself is removed too, since nothing on the request tells the two apart; a credential
meant for another origin needs that origin in ``trusted_origins``. A third-party auth that is not a
``CredentialStamper`` and puts its credential in any other header or in the query is not covered: declare its names by
implementing the protocol.

**An auth flow's own hops** (requests a :class:`~headgate.CachedTokenAuth` mint yields, marked with
:data:`CHAIN_ORIGIN`) carry the credentials the mint needs to the token endpoint the code addressed, so that origin is
trusted for the hop and for the redirects it is answered with, and no other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx2

from headgate._core import CredentialStamper, origin_text

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["CHAIN_ORIGIN", "CredentialGuard"]

#: The request extension holding the origin (as :func:`~headgate._core.origin_text`) a hop chain may carry credentials
#: to besides the trusted ones. An auth flow sets it on each request it yields; httpx2 hands a request's extensions to
#: the redirects that follow it.
CHAIN_ORIGIN: Final = "headgate.chain_origin"

#: The headers withheld from an untrusted origin whatever the client's auth declares.
_ALWAYS_WITHHELD: Final = frozenset({"authorization", "proxy-authorization"})


class CredentialGuard:
    """A request hook removing credentials from any hop addressed to an origin that is not trusted.

    What the auth stamps is read once, when the guard is built.
    """

    __slots__ = ("_headers", "_params", "_trusted")

    def __init__(
        self, *, auth: httpx2.Auth | None, base_url: httpx2.URL, trusted_origins: Iterable[str], cookie_jar: bool
    ) -> None:
        """Trust *base_url*'s origin and *trusted_origins* (normalised origin text); withhold *auth*'s credentials.

        *cookie_jar* says whether the client keeps a cookie jar, in which case ``Cookie`` is left to it.
        """
        headers = set(_ALWAYS_WITHHELD)
        if not cookie_jar:
            headers.add("cookie")
        params = frozenset[str]()
        if isinstance(auth, CredentialStamper):
            headers.update(name.lower() for name in auth.credential_headers)
            params = auth.credential_params
        self._headers = frozenset(headers)
        self._params = params
        self._trusted = frozenset({base_url.origin, *(httpx2.Origin(origin) for origin in trusted_origins)})

    def trusts(self, request: httpx2.Request) -> bool:
        """Return whether *request* may carry credentials: it is addressed to a trusted origin or its chain's own."""
        origin = request.url.origin
        if origin in self._trusted:
            return True
        chain = request.extensions.get(CHAIN_ORIGIN)
        return isinstance(chain, str) and chain == origin_text(origin)

    async def __call__(self, request: httpx2.Request) -> None:
        """Remove the credentials from *request* unless it is addressed to a trusted origin."""
        if self.trusts(request):
            return
        for name in self._headers:
            request.headers.pop(name, None)
        params = request.url.params
        for name in self._params:
            if name in params:
                request.url = request.url.copy_remove_param(name)
