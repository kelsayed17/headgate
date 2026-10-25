# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Keeping credentials out of the cache, and each credential's entries to itself.

hishel persists the request it is handed with every entry (URL and headers) and finds a hit by hashing and comparing
that URL. So the cache layer hands hishel a view of the request, :meth:`CredentialScope.view`, and restores the real one
below hishel for the network:

* **Nothing credential-bearing is stored.** The view drops ``Authorization``, ``Proxy-Authorization``, ``Cookie``,
  every header and query parameter the client's auth declares (:class:`~headgate._core.CredentialStamper`), every
  query parameter :func:`~headgate._url.is_secret_param` names, and the URL's userinfo.
* **What was dropped decides the partition.** Under :attr:`Partition.CREDENTIAL <headgate.cache.Partition.CREDENTIAL>`
  the view's URL carries a partition label in its fragment: an HMAC-SHA256, under a key drawn at random once per
  process, of the auth's class and :meth:`~headgate._core.CredentialStamper.credential_identity`, and of the value of
  every dropped credential the auth did not declare (a caller's own ``Authorization`` header, a cookie, an ``api_key``
  in the URL). Two requests with different credentials therefore never share an entry, and the label can be neither
  reversed nor compared with another process's. A request with no credential at all gets no label, so anonymous
  entries are the same namespace :attr:`Partition.SHARED <headgate.cache.Partition.SHARED>` uses.

The label rides the fragment because a fragment is never sent, and leaves the host, path and query a filter reads
untouched. The auth's own credential is represented by its identity rather than by the header value it stamps, so a
re-minted bearer token keeps its entries. The flip side: a call that overrides the auth's own header with ``headers=``
is partitioned by the auth's identity, not by the value it sent.

The key is per process, so credential-partitioned entries in a persistent storage are not found again after a restart;
they expire by their TTL like any other.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING, Final

import httpx2

from headgate._core import CredentialStamper
from headgate._url import is_secret_param
from headgate.cache._policy import Partition
from headgate.errors import ClientConfigurationError

if TYPE_CHECKING:
    from headgate.cache._policy import HttpCache

__all__ = ["ALWAYS_SECRET_HEADERS", "CredentialScope", "credential_scope"]

#: Headers that carry a credential whatever set them; never stored. Lower case.
ALWAYS_SECRET_HEADERS: Final = frozenset({"authorization", "proxy-authorization", "cookie"})

#: The partition HMAC key: random per process, so a label is neither reversible nor comparable across processes.
_PARTITION_KEY: Final = secrets.token_bytes(32)

#: The fragment prefix that marks a partition label in the URL hishel stores.
_LABEL_PREFIX: Final = "headgate-partition-"


class CredentialScope:
    """One client's rule for what the cache may store of a request and whose entry it is. See the module docstring."""

    __slots__ = ("_declared_headers", "_declared_params", "_partitioned", "_secret_headers", "_stamper")

    def __init__(
        self, stamper: CredentialStamper | None = None, *, partition: Partition = Partition.CREDENTIAL
    ) -> None:
        """Strip what *stamper* declares, and partition by its identity under :attr:`Partition.CREDENTIAL`.

        The names *stamper* declares are read once, here.
        """
        declared = frozenset[str]()
        self._declared_params = frozenset[str]()
        if stamper is not None:
            declared = frozenset(name.lower() for name in stamper.credential_headers)
            self._declared_params = frozenset(stamper.credential_params)
        self._declared_headers = declared
        self._secret_headers = ALWAYS_SECRET_HEADERS | declared
        self._stamper = stamper if partition is Partition.CREDENTIAL else None
        self._partitioned = partition is Partition.CREDENTIAL

    def view(self, request: httpx2.Request) -> tuple[httpx2.URL, httpx2.Headers]:
        """Return the URL and headers hishel gets for *request*: no credential, and its partition label if any."""
        undeclared: list[bytes] = []
        kept_headers: list[tuple[bytes, bytes]] = []
        for raw_name, raw_value in request.headers.raw:
            name = raw_name.decode("latin-1").lower()
            if name not in self._secret_headers:
                kept_headers.append((raw_name, raw_value))
            elif name not in self._declared_headers:
                undeclared.extend((b"header", name.encode("latin-1"), raw_value))
        url = request.url
        params = url.params.multi_items()
        kept_params: list[tuple[str, str]] = []
        for name, value in params:
            if not (name in self._declared_params or is_secret_param(name)):
                kept_params.append((name, value))
            elif name not in self._declared_params:
                undeclared.extend((b"param", name.encode(), value.encode()))
        if url.userinfo:
            undeclared.extend((b"userinfo", url.userinfo))
        stored = url.copy_with(userinfo=b"", fragment=self._label(undeclared))
        if len(kept_params) != len(params):
            stored = stored.copy_with(params=httpx2.QueryParams(tuple(kept_params)))
        return stored, httpx2.Headers(kept_headers)

    def _label(self, undeclared: list[bytes]) -> str | None:
        """Return the fragment naming the request's partition, or ``None`` for a shared or credential-free request."""
        if not self._partitioned:
            return None
        parts = list(undeclared)
        if self._stamper is not None:
            auth = type(self._stamper)
            parts[:0] = (
                b"auth",
                f"{auth.__module__}.{auth.__qualname__}".encode(),
                self._stamper.credential_identity(),
            )
        if not parts:
            return None
        mac = hmac.new(_PARTITION_KEY, digestmod=hashlib.sha256)
        for part in parts:
            # Length-prefixed, so no two different sequences of parts feed the MAC the same bytes.
            mac.update(len(part).to_bytes(8, "big"))
            mac.update(part)
        return _LABEL_PREFIX + mac.hexdigest()


def credential_scope(cache: HttpCache, auth: httpx2.Auth | None) -> CredentialScope:
    """Return the :class:`CredentialScope` for a client with *cache* and *auth*.

    :raises ClientConfigurationError: for an auth that does not declare its credentials under
        :attr:`Partition.CREDENTIAL <headgate.cache.Partition.CREDENTIAL>`, since nothing could keep its entries apart
        from another credential's or its credential out of storage.
    """
    if isinstance(auth, CredentialStamper):
        return CredentialScope(auth, partition=cache.partition)
    if auth is not None and cache.partition is Partition.CREDENTIAL:
        msg = (
            f"the HTTP cache partitions entries by credential, but {type(auth).__qualname__} does not declare its "
            f"credentials (headgate._core.CredentialStamper: credential_headers, credential_params, "
            f"credential_identity()), so another credential's client on the same storage could be served its entries. "
            f"Implement CredentialStamper on the auth, or, only if every cached response is public, pass "
            f"HttpCache(partition=Partition.SHARED)"
        )
        raise ClientConfigurationError(msg)
    return CredentialScope(partition=cache.partition)
