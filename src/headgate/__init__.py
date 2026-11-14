# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""headgate: an async HTTP client with retry, rate limiting, circuit breaking and declared responses.

Built on ``httpx2``; Python 3.14 or newer. An SDK holds a :class:`Client`, configures it with a
:class:`Rate` (or a shared :class:`RateBudget`) and optionally :class:`TransportPolicy` and :class:`HttpOptions`, and
picks a codec: ``headgate.msgspec``, ``headgate.pydantic``, or any library as a :class:`Codec` of its own. Re-exported
here is what a client is written with:

* the client and its options: :class:`Client`, :data:`Method`, :class:`RequestOptions`,
  :class:`CallOptions`, :class:`ViewOptions`, :class:`TransportPolicy`, :class:`HttpOptions`, :class:`SecurityOptions`,
  :class:`Timeouts` and :data:`DEFAULT_TIMEOUTS`, :class:`IdempotencyKeys`, :class:`Route` and its
  :class:`RouteOptions`, :class:`Middleware`, :data:`Next` (what a middleware calls to continue) and
  :func:`call_info` (what a middleware reads of its call);
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
  :class:`RateBudget` and :class:`HttpCache`;
* pages: :meth:`Client.paginate`'s :class:`Paginated` and its pagers;
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
from headgate._client import Client, Method
from headgate._codec import Codec, decode_with, reader_for, writer_for
from headgate._core import call_info
from headgate._middleware import Middleware, Next
from headgate._options import (
    DEFAULT_TIMEOUTS,
    HttpOptions,
    IdempotencyKeys,
    Route,
    RouteOptions,
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
from headgate.breaker._policy import BreakerPolicy
from headgate.cache._policy import HttpCache
from headgate.errors import (
    AuthError,
    ClientError,
    ClientTimeoutError,
    RateLimitError,
    TransportError,
    UpstreamStatusError,
)
from headgate.pagination._paginated import Paginated
from headgate.ratelimit._budget import RateBudget
from headgate.ratelimit._rate import Rate
from headgate.retry._policy import RetryPolicy

__all__ = [
    "DEFAULT_TIMEOUTS",
    "MAX_MINT_RESPONSE_BYTES",
    "AuthError",
    "BearerAuth",
    "Body",
    "BreakerPolicy",
    "CachedTokenAuth",
    "CallOptions",
    "Client",
    "ClientError",
    "ClientTimeoutError",
    "Codec",
    "EncodedBody",
    "Expect",
    "Form",
    "HttpCache",
    "HttpOptions",
    "IdempotencyKeys",
    "Json",
    "Method",
    "Middleware",
    "Minted",
    "Multipart",
    "Next",
    "Paginated",
    "QueryParamAuth",
    "Rate",
    "RateBudget",
    "RateLimitError",
    "Raw",
    "Reader",
    "RequestOptions",
    "ResponseCheck",
    "RetryPolicy",
    "Route",
    "RouteOptions",
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
    "call_info",
    "decode_with",
    "event_stream",
    "nothing",
    "reader_for",
    "writer_for",
]
