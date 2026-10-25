# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The opt-in HTTP response cache, and conditional reads.

A client caches when its ``TransportPolicy(cache=HttpCache(policy=..., storage=..., ttl=...))`` says so. The cache sits
below retry and above the rate limiter, so a hit spends no rate token and is not charged to the circuit breaker; a
storage fault degrades to the network instead of failing the call.

* **Partitioned by credential.** Entries are visible only to requests made with the same credential
  (:attr:`Partition.CREDENTIAL`), so clients with different keys can share one storage safely. Public data can opt into
  :attr:`Partition.SHARED`.
* **Credentials are never stored.** ``Authorization``, ``Proxy-Authorization``, ``Cookie``, every header and query
  parameter the auth declares, every well-known secret query parameter and the URL's userinfo are removed from what is
  stored, and restored, unchanged, for the network.
* **TTL, not revalidation.** A ``FilterPolicy`` cache serves an entry until its TTL ends and never revalidates it.
  :func:`conditional` is the caller-driven alternative: it sends the caller's :class:`Validator` and reads a 304 as
  :class:`NotModified`.
"""

from headgate.cache._conditional import Fresh, NotModified, Validator, conditional
from headgate.cache._policy import HttpCache, MethodFilter, Partition, PathFragmentFilter, StatusCodeFilter

__all__ = [
    "Fresh",
    "HttpCache",
    "MethodFilter",
    "NotModified",
    "Partition",
    "PathFragmentFilter",
    "StatusCodeFilter",
    "Validator",
    "conditional",
]
