# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""headgate: an async HTTP base client with retry, rate limiting, circuit breaking and declared responses.

Built on ``httpx2``; Python 3.14 or newer. A client subclasses :class:`AsyncBaseClient`, configures it with a
:class:`Rate` and optionally :class:`TransportPolicy` and :class:`HttpOptions`, and picks a codec flavor by importing
``headgate.msgspec`` or ``headgate.pydantic``.
"""

from headgate._auth import CachedTokenAuth, Minted, QueryParamAuth, StaticHeaderAuth
from headgate._cache import HttpCache, MethodFilter, PathFragmentFilter, StatusCodeFilter
from headgate._client import AsyncBaseClient, RequestOptions
from headgate._options import HttpOptions, TransportPolicy
from headgate._rate_limit import Rate, RateRule
from headgate._responses import Expect, Reader, RefusalCheck, RefusedError, as_bytes, event_stream, json_reader, nothing
from headgate.errors import (
    AuthError,
    ClientConfigurationError,
    ClientError,
    ClientTimeoutError,
    DecodeError,
    RateLimitError,
    ResponseParseError,
    ResponseTooLargeError,
    TransportError,
    UpstreamStatusError,
)

__all__ = [
    "AsyncBaseClient",
    "AuthError",
    "CachedTokenAuth",
    "ClientConfigurationError",
    "ClientError",
    "ClientTimeoutError",
    "DecodeError",
    "Expect",
    "HttpCache",
    "HttpOptions",
    "MethodFilter",
    "Minted",
    "PathFragmentFilter",
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
    "StaticHeaderAuth",
    "StatusCodeFilter",
    "TransportError",
    "TransportPolicy",
    "UpstreamStatusError",
    "as_bytes",
    "event_stream",
    "json_reader",
    "nothing",
]
