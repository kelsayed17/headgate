# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""headgate: an async HTTP base client with retry, rate limiting, circuit breaking and declared responses.

Built on ``httpx2``; Python 3.14 or newer. A client subclasses :class:`AsyncBaseClient`, configures it with a
:class:`Rate` and optionally :class:`TransportPolicy` and :class:`HttpOptions`, and picks a codec flavor by importing
``headgate.msgspec`` or ``headgate.pydantic``. The policy layers are subpackages with public paths of their own
(:mod:`headgate.retry`, :mod:`headgate.breaker`, :mod:`headgate.ratelimit`); their configuration values are
re-exported here.
"""

from headgate._auth import CachedTokenAuth, Minted, QueryParamAuth, StaticHeaderAuth
from headgate._cache import HttpCache, MethodFilter, PathFragmentFilter, StatusCodeFilter
from headgate._client import AsyncBaseClient, RequestOptions
from headgate._core import Observer
from headgate._middleware import Middleware
from headgate._options import HttpOptions, IdempotencyKeys, Route, Timeouts, TransportPolicy
from headgate._responses import Expect, Reader, RefusalCheck, RefusedError, as_bytes, event_stream, json_reader, nothing
from headgate.breaker import BreakerPolicy, BreakerRegistry
from headgate.errors import (
    AuthError,
    CircuitOpenError,
    ClientConfigurationError,
    ClientError,
    ClientTimeoutError,
    DecodeError,
    Problem,
    RateLimitError,
    ResponseParseError,
    ResponseTooLargeError,
    TransportError,
    UpstreamStatusError,
)
from headgate.ratelimit import Rate, RateRule
from headgate.retry import NO_RETRY, Backoff, RetryPolicy

__all__ = [
    "NO_RETRY",
    "AsyncBaseClient",
    "AuthError",
    "Backoff",
    "BreakerPolicy",
    "BreakerRegistry",
    "CachedTokenAuth",
    "CircuitOpenError",
    "ClientConfigurationError",
    "ClientError",
    "ClientTimeoutError",
    "DecodeError",
    "Expect",
    "HttpCache",
    "HttpOptions",
    "IdempotencyKeys",
    "MethodFilter",
    "Middleware",
    "Minted",
    "Observer",
    "PathFragmentFilter",
    "Problem",
    "QueryParamAuth",
    "Rate",
    "RateLimitError",
    "RateRule",
    "Reader",
    "RefusalCheck",
    "RefusedError",
    "RequestOptions",
    "ResponseParseError",
    "ResponseTooLargeError",
    "RetryPolicy",
    "Route",
    "StaticHeaderAuth",
    "StatusCodeFilter",
    "Timeouts",
    "TransportError",
    "TransportPolicy",
    "UpstreamStatusError",
    "as_bytes",
    "event_stream",
    "json_reader",
    "nothing",
]
