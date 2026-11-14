# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Credentials stay on trusted origins: one request hook, run before every hop.

A credential may be sent only to ``base_url``'s origin and the origins in ``SecurityOptions.trusted_origins``. httpx2
runs request hooks after the auth flow has stamped the request and before every hop it sends, so
:class:`CredentialGuard` sees the first request (an absolute cursor URL included), every redirect hop and every hop an
auth flow yields. On any of them addressed elsewhere it removes:

* every header and query parameter the client's auth declares it stamps (a
  :class:`~headgate._core.CredentialStamper`: every auth in headgate);
* ``Authorization`` and ``Proxy-Authorization``, whatever the auth, so a third-party auth or a caller's own header does
  not reach another origin either. httpx2 itself drops ``Authorization`` only on a cross-origin redirect, not on a
  first request to an absolute URL;
* ``Cookie``. A client that keeps a cookie jar (``HttpOptions(cookies=True)``) then gets back what the jar holds for
  the hop's origin, since the jar scopes the cookies it stores to their domains; a ``Cookie`` header set explicitly
  (client-wide or on the call) is not the jar's, and stays off.
* every header the client sets on all its requests (``HttpOptions.headers``), where a static key is often configured,
  except ``User-Agent``, ``Content-Type`` and the content negotiation headers (``Accept``, ``Accept-Encoding``,
  ``Accept-Language``): those name the client, the body and the format it wants, never a caller, and the request would
  be malformed without them.
* the same, for every header a view sets (``with_options(headers=...)``), and for every header the call set itself
  (``headers=``) on a hop addressed to another origin than the one the call named: a redirect, or a later page of a
  walk, whose URL the server chose (:class:`~headgate._core.ExplicitHeaders`). A call that names another origin's URL
  itself still sends it its own headers.

The request is still sent, unsigned: a provider may hand out a signed download URL on another host. A header of one of
those names that the call set itself is removed too, since nothing on the request tells the two apart; a credential
meant for another origin needs that origin in ``trusted_origins``. A third-party auth that is not a
``CredentialStamper`` and puts its credential in any other header or in the query is not covered: declare its names by
implementing the protocol.

**An auth flow's own hops** (requests a :class:`~headgate.CachedTokenAuth` mint yields, marked with
:data:`CHAIN_ORIGIN`) carry the credentials the mint needs to the token endpoint the code addressed, so that origin is
trusted for the hop and for the redirects it is answered with on that origin. A redirect to any other origin is refused
as :class:`~headgate.errors.ForbiddenDestinationError` before it is sent, rather than followed unsigned: a mint's
credentials are not only what the guard knows to remove (a client secret in a header of the flow's own, or in the form
body, which httpx2 sends again on a 307 or 308).

**A middleware's credentials**, and a :class:`~headgate.SigningAuth`'s signature, follow the same rule. Both are
stamped inside the transport stack, once per attempt, after the request hooks, so they would reach a cross-origin
redirect hop after the hook had checked it. The signature is one of the auth's declared credentials; a middleware
declares what it stamps (``credential_headers`` and ``credential_params``, a :class:`~headgate._core.CredentialCarrier`;
``HttpOptions`` refuses one that does not), and those names are removed from any hop not addressed to ``base_url``'s
origin or a trusted one: the guard runs a second time below the last middleware and the signature, where their output
is visible. That pass trusts no auth flow's chain origin for a middleware's credentials, since the middleware did not
choose it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx2

from headgate._core import EXPLICIT_HEADERS, CredentialStamper, ExplicitHeaders, origin_text
from headgate.errors import ForbiddenDestinationError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from http.cookiejar import CookieJar

    from headgate._middleware import Middleware
    from headgate._options import HttpOptions

__all__ = ["CHAIN_ORIGIN", "CredentialGuard", "check_names"]

#: The request extension holding the origin (as :func:`~headgate._core.origin_text`) a hop chain may carry credentials
#: to besides the trusted ones. An auth flow sets it on each request it yields; httpx2 hands a request's extensions to
#: the redirects that follow it.
CHAIN_ORIGIN: Final = "headgate.chain_origin"

#: The headers withheld from an untrusted origin whatever the client's auth declares.
_ALWAYS_WITHHELD: Final = frozenset({"authorization", "proxy-authorization", "cookie"})

#: The client-wide headers still sent to an untrusted origin: they name the client, the body and the format it wants,
#: never a caller. Lower case.
_NEVER_CREDENTIALS: Final = frozenset({"user-agent", "content-type", "accept", "accept-encoding", "accept-language"})


def check_names(owner: str, **declared: object) -> None:
    """Raise ``TypeError`` unless every value in *declared* is a frozenset of names (``str``), naming *owner*.

    A bare ``str`` iterates as its letters, so ``credential_headers = "x-api-key"`` would withhold no real header.
    """
    for attribute, names in declared.items():
        if not (isinstance(names, frozenset) and all(isinstance(name, str) for name in names)):
            msg = f"{owner}.{attribute} must be a frozenset of names, not {type(names).__name__}"
            raise TypeError(msg)


class CredentialGuard:
    """A request hook removing credentials from any hop addressed to an origin that is not trusted.

    What the auth and the middleware stamp is read once, when the guard is built. The client runs it as a request
    hook and, when it has middleware or a ``SigningAuth``, again below the last middleware and the signature (see the
    module docstring).
    """

    __slots__ = ("_cookie_jar", "_headers", "_middleware_headers", "_middleware_params", "_params", "_trusted")

    def __init__(
        self,
        *,
        auth: httpx2.Auth | None,
        base_url: httpx2.URL,
        http: HttpOptions,
        middleware: Sequence[Middleware] = (),
        cookie_jar: CookieJar | None,
    ) -> None:
        """Trust *base_url*'s origin and *http*'s ``trusted_origins``; withhold what *auth*, *http*, *middleware* carry.

        *http*'s are each header in its ``headers`` but ``User-Agent``, ``Content-Type`` and the content negotiation
        headers, and *middleware*'s what each declares. *cookie_jar* is the jar the client keeps, if any, whose
        cookies for the hop's origin replace a withheld ``Cookie``.

        :raises TypeError: for an *auth* whose declarations are not frozensets of names.
        """
        headers = set(_ALWAYS_WITHHELD)
        headers.update({name.lower() for name in http.headers} - _NEVER_CREDENTIALS)
        params = frozenset[str]()
        if isinstance(auth, CredentialStamper):
            check_names(
                type(auth).__name__,
                credential_headers=auth.credential_headers,
                credential_params=auth.credential_params,
            )
            headers.update(name.lower() for name in auth.credential_headers)
            params = auth.credential_params
        self._headers = frozenset(headers)
        self._params = params
        self._cookie_jar = cookie_jar
        self._middleware_headers = frozenset(name.lower() for item in middleware for name in item.credential_headers)
        self._middleware_params = frozenset(name for item in middleware for name in item.credential_params)
        trusted = (httpx2.Origin(origin) for origin in http.security.trusted_origins)
        self._trusted = frozenset({base_url.origin, *trusted})

    def trusts(self, request: httpx2.Request) -> bool:
        """Return whether *request* may carry credentials: it is addressed to a trusted origin or its chain's own."""
        origin = request.url.origin
        if origin in self._trusted:
            return True
        chain = request.extensions.get(CHAIN_ORIGIN)
        return isinstance(chain, str) and chain == origin_text(origin)

    async def __call__(self, request: httpx2.Request) -> None:
        """Remove the credentials from *request* unless it is addressed to a trusted origin.

        The middleware's are removed unless the origin is ``base_url``'s or a trusted one; an auth flow's chain origin
        is trusted for the auth's credentials only.

        :raises ForbiddenDestinationError: for an auth flow's hop redirected off its chain origin.
        """
        origin = request.url.origin
        chain = request.extensions.get(CHAIN_ORIGIN)
        if isinstance(chain, str) and chain != origin_text(origin):
            msg = (
                f"refused to follow a token mint's redirect from {chain} to {origin_text(origin)}: a mint hop's "
                f"credentials (its headers and body) go only to the origin its flow addressed. A flow that means to "
                f"go there yields that request itself"
            )
            raise ForbiddenDestinationError(msg)
        if origin not in self._trusted:
            _remove(request, self._middleware_headers, self._middleware_params)
        if not self.trusts(request):
            _remove(request, self._headers, self._params)
            explicit = request.extensions.get(EXPLICIT_HEADERS)
            if isinstance(explicit, ExplicitHeaders):
                _remove(request, explicit.withheld_at(origin) - _NEVER_CREDENTIALS, frozenset())
            if self._cookie_jar is not None:
                httpx2.Cookies(self._cookie_jar).set_cookie_header(request)


def _remove(request: httpx2.Request, headers: frozenset[str], params: frozenset[str]) -> None:
    """Remove *headers* and the query parameters *params* from *request*."""
    for name in headers:
        request.headers.pop(name, None)
    present = request.url.params
    for name in params:
        if name in present:
            request.url = request.url.copy_remove_param(name)
