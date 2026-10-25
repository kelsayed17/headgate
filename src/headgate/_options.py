# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The two option groups an :class:`~headgate.AsyncBaseClient` is configured with, beyond its URL, rate and auth.

* :class:`TransportPolicy`: what the transport stack does for every request (retries, the breaker, per-path rates, the
  cache). These are the policies ``headgate.policy.policy_disabled()`` switches off, the cache aside.
* :class:`HttpOptions`: how a request is sent and its response read (the network transport, headers, timeout,
  redirects, the body cap, a host's refusal check).

Both are frozen and validate on construction, so a bad configuration fails where it is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from headgate._body import DEFAULT_MAX_RESPONSE_BYTES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import httpx2

    from headgate._cache import HttpCache
    from headgate._rate_limit import RateRule
    from headgate._responses import RefusalCheck

__all__ = ["HttpOptions", "TransportPolicy"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TransportPolicy:
    """What the transport stack does for every request.

    * ``max_attempts``: sends per request, the first included, so ``1`` never retries. Every method is retried;
      a non-idempotent call opts out with ``retry=False``.
    * ``circuit_breaker``: whether this client consults the process-wide per-host breaker.
    * ``rate_limits``: a second rate for the requests whose path matches a rule (see :class:`~headgate.RateRule`).
    * ``cache``: an opt-in :class:`~headgate.HttpCache`.
    """

    max_attempts: int = 3
    circuit_breaker: bool = True
    rate_limits: Sequence[RateRule] = ()
    cache: HttpCache | None = None

    def __post_init__(self) -> None:
        """Refuse an attempt count below one."""
        if self.max_attempts < 1:
            msg = f"max_attempts must be >= 1 (got {self.max_attempts})"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpOptions:
    """How a request is sent and its response read.

    * ``transport``: the network transport under the policy stack. The default is an HTTP/2 pool of 10 connections
      (5 keep-alive); a client budgeted above ~30 req/s, or one that needs another TLS stack
      (``headgate.curl.AsyncCurlTransport``), passes its own.
    * ``headers``: sent on every request; a call's ``headers=`` wins a clash.
    * ``timeout``: the read timeout in seconds (connect and write are 10 s, pool 15 s).
    * ``follow_redirects``: the client's default; a call can override it.
    * ``max_response_bytes``: the cap on a buffered body, counted after decompression.
    * ``refusal_check``: a per-host check run on every final response (see :data:`~headgate.RefusalCheck`). It sees
      only the final response, so a refusal written on a followed hop would be skipped, and it therefore requires
      ``follow_redirects=False``.
    """

    transport: httpx2.AsyncBaseTransport | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    follow_redirects: bool = True
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    refusal_check: RefusalCheck | None = None

    def __post_init__(self) -> None:
        """Refuse a non-positive timeout or cap, and a refusal check on a redirect-following client."""
        if self.timeout <= 0:
            msg = f"timeout must be > 0 (got {self.timeout})"
            raise ValueError(msg)
        if self.max_response_bytes <= 0:
            msg = f"max_response_bytes must be > 0 (got {self.max_response_bytes})"
            raise ValueError(msg)
        if self.refusal_check is not None and self.follow_redirects:
            msg = "an HttpOptions with a refusal_check= must set follow_redirects=False"
            raise ValueError(msg)
