# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Keeping credentials out of the cache, and each credential's entries to itself.

hishel persists the request it is handed with every entry (URL and headers) and finds a hit by hashing and comparing
that URL. So the cache layer hands hishel a view of the request, :meth:`CredentialScope.view`, and restores the real one
below hishel for the network:

* **Nothing credential-bearing is stored.** The view drops ``Authorization``, ``Proxy-Authorization``, ``Cookie``,
  every header and query parameter the client's auth (:class:`~headgate._core.CredentialStamper`) or middleware
  (:class:`~headgate._core.CredentialCarrier`) declares, every query parameter :func:`~headgate._url.is_secret_param`
  names, and the URL's userinfo. Every header it keeps carries a keyed digest in place of its value, so a credential in
  a header nothing declares (an undeclared auth's, a client-wide or a per-call one) is not stored either, while
  hishel's ``Vary`` matching, which compares a stored value with a new one, still works.
* **What was dropped decides the partition.** Under :attr:`Partition.CREDENTIAL <headgate.cache.Partition.CREDENTIAL>`
  the view's URL carries a partition label in its fragment: an HMAC-SHA256, under a key drawn at random once per
  process, of the auth's class and :meth:`~headgate._core.CredentialStamper.credential_identity`, of the value of every
  dropped credential the auth did not declare (a middleware's, a caller's own ``Authorization`` header, a cookie, an
  ``api_key`` in the URL), and of the value of every header the client sets on all its requests
  (``HttpOptions.headers``, where a static key is often configured). Two requests with different credentials therefore
  never share an entry, and the label can be neither reversed nor compared with another process's. A request with no
  credential at all gets no label, so anonymous entries are the same namespace
  :attr:`Partition.SHARED <headgate.cache.Partition.SHARED>` uses.
* **A secret-named parameter still names a resource.** Under either partition, a dropped query parameter the auth did
  not declare labels the view by its value: ``key`` and ``session`` are well-known secret names, but
  ``/config?key=theme`` and ``?key=locale`` are different resources, which would otherwise share one entry.

The label rides the fragment because a fragment is never sent, and leaves the host, path and query a filter reads
untouched. The auth's own credential is represented by its identity rather than by the header value it stamps, so a
re-minted bearer token keeps its entries; a middleware that is a ``CredentialStamper`` too is represented by its
identity the same way, so a signer whose signature changes on every request still finds its entries. The flip side: a
call that overrides the auth's own header with ``headers=`` is partitioned by the auth's identity, not by the value it
sent.

The key is per process, so credential-partitioned entries in a persistent storage are not found again after a restart,
nor is an entry whose response varies on a request header (``Vary``); they expire by their TTL like any other.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING, Final

import httpx2

from headgate._core import CredentialCarrier, CredentialStamper
from headgate._url import is_secret_param
from headgate.cache._policy import Partition
from headgate.errors import ClientConfigurationError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from headgate.cache._policy import HttpCache

__all__ = ["ALWAYS_SECRET_HEADERS", "CredentialScope", "credential_scope"]

#: Headers that carry a credential whatever set them; never stored. Lower case.
ALWAYS_SECRET_HEADERS: Final = frozenset({"authorization", "proxy-authorization", "cookie"})

#: The key of partition labels and header digests: random per process, so neither is reversible nor comparable across
#: processes.
_PARTITION_KEY: Final = secrets.token_bytes(32)

#: The fragment prefix that marks a partition label in the URL hishel stores.
_LABEL_PREFIX: Final = "headgate-partition-"


class CredentialScope:
    """One client's rule for what the cache may store of a request and whose entry it is. See the module docstring."""

    __slots__ = (
        "_client_headers",
        "_dropped_headers",
        "_dropped_params",
        "_identities",
        "_partitioned",
        "_represented_headers",
        "_represented_params",
    )

    def __init__(
        self,
        stamper: CredentialStamper | None = None,
        *,
        partition: Partition = Partition.CREDENTIAL,
        carriers: Iterable[object] = (),
        client_headers: Iterable[str] = (),
    ) -> None:
        """Strip what *stamper* and *carriers* declare, and partition by their credentials under the default partition.

        *stamper* is the client's auth, represented by its identity. Each of *carriers* (the client's middleware) that
        is a :class:`~headgate._core.CredentialCarrier` has what it declares dropped too, represented by its identity
        when it is a :class:`~headgate._core.CredentialStamper` and by the values otherwise. *client_headers* names the
        headers the client sets on every request, which label the partition by their values. The names are read once,
        here.
        """
        sources = [carrier for carrier in carriers if isinstance(carrier, CredentialCarrier)]
        if stamper is not None:
            sources.insert(0, stamper)
        identities: list[bytes] = []
        dropped_headers, dropped_params = set(ALWAYS_SECRET_HEADERS), set[str]()
        represented_headers, represented_params = set[str](), set[str]()
        for source in sources:
            headers = {name.lower() for name in source.credential_headers}
            params = set(source.credential_params)
            dropped_headers |= headers
            dropped_params |= params
            if isinstance(source, CredentialStamper):
                kind = type(source)
                identities += (b"auth", f"{kind.__module__}.{kind.__qualname__}".encode(), source.credential_identity())
                represented_headers |= headers
                represented_params |= params
        self._identities = tuple(identities)
        self._dropped_headers = frozenset(dropped_headers)
        self._dropped_params = frozenset(dropped_params)
        self._represented_headers = frozenset(represented_headers)
        self._represented_params = frozenset(represented_params)
        self._client_headers = frozenset(name.lower() for name in client_headers) - self._dropped_headers
        self._partitioned = partition is Partition.CREDENTIAL

    def view(self, request: httpx2.Request) -> tuple[httpx2.URL, httpx2.Headers]:
        """Return the URL and headers hishel gets for *request*: no credential, and its partition label if any."""
        credentials: list[bytes] = []
        kept_headers: list[tuple[bytes, bytes]] = []
        for raw_name, raw_value in request.headers.raw:
            name = raw_name.decode("latin-1").lower()
            if name not in self._dropped_headers:
                kept_headers.append((raw_name, _digest(b"value", name.encode("latin-1"), raw_value).encode()))
                if name in self._client_headers:
                    credentials.extend((b"client", name.encode("latin-1"), raw_value))
            elif name not in self._represented_headers:
                credentials.extend((b"header", name.encode("latin-1"), raw_value))
        url = request.url
        params = url.params.multi_items()
        kept_params: list[tuple[str, str]] = []
        secret_params: list[bytes] = []
        for name, value in params:
            if not (name in self._dropped_params or is_secret_param(name)):
                kept_params.append((name, value))
            elif name not in self._represented_params:
                secret_params.extend((b"param", name.encode(), value.encode()))
        if url.userinfo:
            credentials.extend((b"userinfo", url.userinfo))
        stored = url.copy_with(userinfo=b"", fragment=self._label(credentials, secret_params))
        if len(kept_params) != len(params):
            stored = stored.copy_with(params=httpx2.QueryParams(tuple(kept_params)))
        return stored, httpx2.Headers(kept_headers)

    def _label(self, credentials: list[bytes], secret_params: list[bytes]) -> str | None:
        """Return the fragment naming the request's partition, or ``None`` for a request with nothing to keep apart.

        Under :attr:`Partition.SHARED <headgate.cache.Partition.SHARED>` only *secret_params* count, since they may be
        ordinary parameters that happen to have a secret name.
        """
        parts = [*self._identities, *credentials, *secret_params] if self._partitioned else secret_params
        if not parts:
            return None
        return _LABEL_PREFIX + _digest(*parts)


def _digest(*parts: bytes) -> str:
    """Return the HMAC-SHA256 of *parts* under the per-process key, as hex."""
    mac = hmac.new(_PARTITION_KEY, digestmod=hashlib.sha256)
    for part in parts:
        # Length-prefixed, so no two different sequences of parts feed the MAC the same bytes.
        mac.update(len(part).to_bytes(8, "big"))
        mac.update(part)
    return mac.hexdigest()


def credential_scope(
    cache: HttpCache, auth: httpx2.Auth | None, *, middleware: Iterable[object] = (), headers: Iterable[str] = ()
) -> CredentialScope:
    """Return the :class:`CredentialScope` for a client with *cache*, *auth*, *middleware* and client-wide *headers*.

    :raises ClientConfigurationError: for an auth that does not declare its credentials under
        :attr:`Partition.CREDENTIAL <headgate.cache.Partition.CREDENTIAL>`, since nothing could keep its entries apart
        from another credential's or its credential out of storage.
    """
    stamper = auth if isinstance(auth, CredentialStamper) else None
    if auth is not None and stamper is None and cache.partition is Partition.CREDENTIAL:
        msg = (
            f"the HTTP cache partitions entries by credential, but {type(auth).__qualname__} does not declare its "
            f"credentials (headgate._core.CredentialStamper: credential_headers, credential_params, "
            f"credential_identity()), so another credential's client on the same storage could be served its entries. "
            f"Implement CredentialStamper on the auth, or, only if every cached response is public, pass "
            f"HttpCache(partition=Partition.SHARED)"
        )
        raise ClientConfigurationError(msg)
    return CredentialScope(stamper, partition=cache.partition, carriers=middleware, client_headers=headers)
