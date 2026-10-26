# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""headgate: an async HTTP base client with retry, rate limiting, circuit breaking and declared responses.

Built on ``httpx2``; Python 3.14 or newer. A client subclasses :class:`AsyncBaseClient`, configures it with a
:class:`Rate` (or a shared :class:`RateBudget`) and optionally :class:`TransportPolicy` and :class:`HttpOptions`, and
picks a codec flavor by importing ``headgate.msgspec`` or ``headgate.pydantic``. Re-exported here:

* the client and its options: :class:`AsyncBaseClient`, :data:`Method`, :class:`RequestOptions`,
  :class:`TransportPolicy`, :class:`HttpOptions`, :class:`Timeouts`, :class:`IdempotencyKeys`, :class:`Route`,
  :class:`Middleware`, :class:`Observer`;
* declared responses: :class:`Expect`, :data:`Reader` and the readers, :data:`RefusalCheck`, :class:`RefusedError`;
* auth: :class:`StaticHeaderAuth`, :class:`QueryParamAuth`, :class:`CachedTokenAuth`, :class:`Minted`,
  :data:`MAX_MINT_RESPONSE_BYTES`;
* the policy values: retry (:class:`RetryPolicy`, :class:`Backoff`, :class:`RetryBudget`, :class:`Hedge`,
  :data:`NO_RETRY`), the breaker (:class:`BreakerPolicy`, :class:`BreakerRegistry`), rate limiting (:class:`Rate`,
  :class:`RateBudget`, :class:`RateRule`, :class:`Adaptive`) and the cache (:class:`HttpCache`, its filters,
  :class:`Partition`, :func:`conditional`);
* streams and pages: :class:`Stream`, :meth:`AsyncBaseClient.paginate`'s :class:`Paginated` and its pagers;
* the errors, all under :class:`ClientError`.

The policy layers are subpackages with public paths of their own (:mod:`headgate.retry`, :mod:`headgate.breaker`,
:mod:`headgate.ratelimit`, :mod:`headgate.cache`, :mod:`headgate.security`, :mod:`headgate.streaming`,
:mod:`headgate.pagination`); events are in :mod:`headgate.events` and the testing kit in :mod:`headgate.testing`.
"""

from headgate._auth import MAX_MINT_RESPONSE_BYTES, CachedTokenAuth, Minted, QueryParamAuth, StaticHeaderAuth
from headgate._client import AsyncBaseClient, Method, RequestOptions
from headgate._core import Observer
from headgate._middleware import Middleware
from headgate._options import HttpOptions, IdempotencyKeys, Route, Timeouts, TransportPolicy
from headgate._responses import Expect, Reader, RefusalCheck, RefusedError, as_bytes, event_stream, json_reader, nothing
from headgate.breaker import BreakerPolicy, BreakerRegistry
from headgate.cache import (
    Fresh,
    HttpCache,
    MethodFilter,
    NotModified,
    Partition,
    PathFragmentFilter,
    StatusCodeFilter,
    Validator,
    conditional,
)
from headgate.errors import (
    AuthError,
    CircuitOpenError,
    ClientConfigurationError,
    ClientError,
    ClientTimeoutError,
    DecodeError,
    ForbiddenDestinationError,
    PageLimitError,
    Problem,
    RateLimitError,
    ResponseParseError,
    ResponseTooLargeError,
    TransportError,
    UpstreamStatusError,
)
from headgate.pagination import Cursor, LinkHeader, NextUrl, Offset, PageLimit, Pager, PageRequest, Paginated
from headgate.ratelimit import Adaptive, Rate, RateBudget, RateRule
from headgate.retry import NO_RETRY, Backoff, Hedge, RetryBudget, RetryPolicy
from headgate.streaming import Stream

__all__ = [
    "MAX_MINT_RESPONSE_BYTES",
    "NO_RETRY",
    "Adaptive",
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
    "Cursor",
    "DecodeError",
    "Expect",
    "ForbiddenDestinationError",
    "Fresh",
    "Hedge",
    "HttpCache",
    "HttpOptions",
    "IdempotencyKeys",
    "LinkHeader",
    "Method",
    "MethodFilter",
    "Middleware",
    "Minted",
    "NextUrl",
    "NotModified",
    "Observer",
    "Offset",
    "PageLimit",
    "PageLimitError",
    "PageRequest",
    "Pager",
    "Paginated",
    "Partition",
    "PathFragmentFilter",
    "Problem",
    "QueryParamAuth",
    "Rate",
    "RateBudget",
    "RateLimitError",
    "RateRule",
    "Reader",
    "RefusalCheck",
    "RefusedError",
    "RequestOptions",
    "ResponseParseError",
    "ResponseTooLargeError",
    "RetryBudget",
    "RetryPolicy",
    "Route",
    "StaticHeaderAuth",
    "StatusCodeFilter",
    "Stream",
    "Timeouts",
    "TransportError",
    "TransportPolicy",
    "UpstreamStatusError",
    "Validator",
    "as_bytes",
    "conditional",
    "event_stream",
    "json_reader",
    "nothing",
]
