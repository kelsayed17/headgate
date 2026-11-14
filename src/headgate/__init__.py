# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""headgate: an async HTTP base client with retry, rate limiting, circuit breaking and declared responses.

Built on ``httpx2``; Python 3.14 or newer. A client subclasses :class:`AsyncBaseClient`, configures it with a
:class:`Rate` (or a shared :class:`RateBudget`) and optionally :class:`TransportPolicy` and :class:`HttpOptions`, and
picks a codec: ``headgate.msgspec``, ``headgate.pydantic``, or any library as a :class:`Codec` of its own. Re-exported
here is what a client is written with:

* the client and its options: :class:`AsyncBaseClient`, :data:`Method`, :class:`RequestOptions`,
  :class:`CallOptions`, :class:`ViewOptions`, :class:`TransportPolicy`, :class:`HttpOptions`, :class:`SecurityOptions`,
  :class:`Timeouts` and :data:`DEFAULT_TIMEOUTS`, :class:`IdempotencyKeys`, :class:`Route`, :class:`Middleware` and
  :data:`Next` (what a middleware calls to continue);
* declared responses: :class:`Expect`, :class:`Reader` and the readers (:data:`as_json` among them),
  :data:`ResponseCheck`;
* codecs: :class:`Codec`, and :func:`reader_for`, :func:`writer_for` and :func:`decode_with`, which build a reader, a
  writer and a decode on any codec;
* request bodies: :data:`Body`, one of :class:`Json`, :class:`Form`, :class:`Multipart`, :class:`Raw` and a
  :data:`Writer`'s :class:`EncodedBody`;
* auth: :class:`BearerAuth`, :class:`StaticHeaderAuth`, :class:`QueryParamAuth`, :class:`SigningAuth`,
  :class:`CachedTokenAuth`,
  :class:`Minted`, :data:`MAX_MINT_RESPONSE_BYTES`;
* the policy values a client usually sets: :class:`RetryPolicy`, :class:`BreakerPolicy`, :class:`Rate`,
  :class:`RateBudget`, :class:`RateRule` and :class:`HttpCache`;
* pages: :meth:`AsyncBaseClient.paginate`'s :class:`Paginated`, its pagers and :class:`PageLimit`;
* the errors, all under :class:`ClientError`.

Everything else lives once, in its layer's subpackage: the finer policy values (:mod:`headgate.retry`'s ``Backoff``,
``Hedge`` and ``RetryBudget``; :mod:`headgate.ratelimit`'s ``Adaptive``; :mod:`headgate.breaker`'s
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
    SigningAuth,
    StaticHeaderAuth,
)
from headgate._call_options import CallOptions, RequestOptions
from headgate._client import AsyncBaseClient, Method
from headgate._codec import Codec, decode_with, reader_for, writer_for
from headgate._middleware import Middleware, Next
from headgate._options import (
    DEFAULT_TIMEOUTS,
    HttpOptions,
    IdempotencyKeys,
    Route,
    SecurityOptions,
    Timeouts,
    TransportPolicy,
)
from headgate._request_body import Body, EncodedBody, Form, Json, Multipart, Raw, Writer
from headgate._responses import (
    Expect,
    Reader,
    ResponseCheck,
    as_bytes,
    as_json,
    body_reader,
    event_stream,
    nothing,
)
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
from headgate.pagination import (
    BodyCursor,
    BodyOffset,
    Cursor,
    LinkHeader,
    NextUrl,
    Offset,
    PageLimit,
    PageNumber,
    Paginated,
)
from headgate.ratelimit import Rate, RateBudget, RateRule
from headgate.retry import RetryPolicy

__all__ = [
    "DEFAULT_TIMEOUTS",
    "MAX_MINT_RESPONSE_BYTES",
    "AsyncBaseClient",
    "AuthError",
    "BearerAuth",
    "Body",
    "BodyCursor",
    "BodyOffset",
    "BreakerPolicy",
    "CachedTokenAuth",
    "CallOptions",
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
    "Form",
    "HttpCache",
    "HttpOptions",
    "IdempotencyKeys",
    "Json",
    "LinkHeader",
    "Method",
    "Middleware",
    "Minted",
    "Multipart",
    "Next",
    "NextUrl",
    "Offset",
    "PageLimit",
    "PageLimitError",
    "PageNumber",
    "Paginated",
    "Problem",
    "QueryParamAuth",
    "Rate",
    "RateBudget",
    "RateLimitError",
    "RateRule",
    "Raw",
    "Reader",
    "RequestOptions",
    "ResponseCheck",
    "ResponseParseError",
    "ResponseTooLargeError",
    "RetryPolicy",
    "Route",
    "SecurityOptions",
    "SigningAuth",
    "StaticHeaderAuth",
    "Timeouts",
    "TransportError",
    "TransportPolicy",
    "UpstreamStatusError",
    "ViewOptions",
    "Writer",
    "as_bytes",
    "as_json",
    "body_reader",
    "decode_with",
    "event_stream",
    "nothing",
    "reader_for",
    "writer_for",
]
