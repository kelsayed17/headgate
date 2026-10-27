# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""headgate: an async HTTP base client with retry, rate limiting, circuit breaking and declared responses.

Built on ``httpx2``; Python 3.14 or newer. A client subclasses :class:`AsyncBaseClient`, configures it with a
:class:`Rate` (or a shared :class:`RateBudget`) and optionally :class:`TransportPolicy` and :class:`HttpOptions`, and
picks a codec: ``headgate.msgspec``, ``headgate.pydantic``, or any library as a :class:`Codec` of its own. Re-exported
here is what a client is written with:

* the client and its options: :class:`AsyncBaseClient`, :data:`Method`, :class:`RequestOptions`,
  :class:`PageOptions`, :class:`ViewOptions`, :class:`TransportPolicy`, :class:`HttpOptions`, :class:`SecurityOptions`,
  :class:`Timeouts`, :class:`IdempotencyKeys`, :class:`Route`, :class:`Middleware` and :data:`Next` (what a
  middleware calls to continue);
* declared responses: :class:`Expect`, :data:`Reader` and the readers, :data:`RefusalCheck`, :class:`RefusedError`;
* codecs: :class:`Codec`, and :func:`decoder_for`, :func:`encoder_for` and :func:`decode_with`, which build a reader, a
  writer and a decode on any codec;
* request bodies: :class:`EncodedBody`, :data:`Writer` and :func:`json_writer`;
* auth: :class:`BearerAuth`, :class:`StaticHeaderAuth`, :class:`QueryParamAuth`, :class:`CachedTokenAuth`,
  :class:`Minted`, :data:`MAX_MINT_RESPONSE_BYTES`;
* the policy values a client usually sets: :class:`RetryPolicy`, :class:`BreakerPolicy`, :class:`Rate`,
  :class:`RateBudget`, :class:`RateRule` and :class:`HttpCache`;
* pages: :meth:`AsyncBaseClient.paginate`'s :class:`Paginated`, its pagers and :class:`PageLimit`;
* the errors, all under :class:`ClientError`.

Everything else lives once, in its layer's subpackage: the finer policy values (:mod:`headgate.retry`'s ``Backoff``,
``Hedge``, ``RetryBudget`` and ``NO_RETRY``; :mod:`headgate.ratelimit`'s ``Adaptive``; :mod:`headgate.breaker`'s
``BreakerRegistry``), the cache's filters, partitions and conditional reads (:mod:`headgate.cache`), what a custom
pager is written with (:mod:`headgate.pagination`), ``Stream`` (:mod:`headgate.streaming`), :mod:`headgate.security`,
events and their ``Observer`` (:mod:`headgate.events`) and the testing kit (:mod:`headgate.testing`).
"""

from headgate._auth import (
    MAX_MINT_RESPONSE_BYTES,
    BearerAuth,
    CachedTokenAuth,
    Minted,
    QueryParamAuth,
    StaticHeaderAuth,
)
from headgate._client import AsyncBaseClient, Method, PageOptions, RequestOptions
from headgate._codec import Codec, decode_with, decoder_for, encoder_for
from headgate._middleware import Middleware, Next
from headgate._options import HttpOptions, IdempotencyKeys, Route, SecurityOptions, Timeouts, TransportPolicy
from headgate._request_body import EncodedBody, Writer, json_writer
from headgate._responses import Expect, Reader, RefusalCheck, RefusedError, as_bytes, event_stream, json_reader, nothing
from headgate._view import ViewOptions
from headgate.breaker import BreakerPolicy
from headgate.cache import HttpCache
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
from headgate.pagination import Cursor, LinkHeader, NextUrl, Offset, PageLimit, Paginated
from headgate.ratelimit import Rate, RateBudget, RateRule
from headgate.retry import RetryPolicy

__all__ = [
    "MAX_MINT_RESPONSE_BYTES",
    "AsyncBaseClient",
    "AuthError",
    "BearerAuth",
    "BreakerPolicy",
    "CachedTokenAuth",
    "CircuitOpenError",
    "ClientConfigurationError",
    "ClientError",
    "ClientTimeoutError",
    "Codec",
    "Cursor",
    "DecodeError",
    "EncodedBody",
    "Expect",
    "ForbiddenDestinationError",
    "HttpCache",
    "HttpOptions",
    "IdempotencyKeys",
    "LinkHeader",
    "Method",
    "Middleware",
    "Minted",
    "Next",
    "NextUrl",
    "Offset",
    "PageLimit",
    "PageLimitError",
    "PageOptions",
    "Paginated",
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
    "RetryPolicy",
    "Route",
    "SecurityOptions",
    "StaticHeaderAuth",
    "Timeouts",
    "TransportError",
    "TransportPolicy",
    "UpstreamStatusError",
    "ViewOptions",
    "Writer",
    "as_bytes",
    "decode_with",
    "decoder_for",
    "encoder_for",
    "event_stream",
    "json_reader",
    "json_writer",
    "nothing",
]
