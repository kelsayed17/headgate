"""headgate: an async HTTP base client with retry, rate limiting, circuit breaking and declared responses.

Built on ``httpx2``; Python 3.14 or newer. A client subclasses :class:`AsyncBaseClient` and picks a codec flavor by
importing ``headgate.msgspec`` or ``headgate.pydantic``.
"""

from headgate._auth import CachedTokenAuth, Minted, QueryParamAuth, StaticHeaderAuth
from headgate._base import AsyncBaseClient
from headgate._cache import HttpCache, MethodFilter, PathFragmentFilter, StatusCodeFilter
from headgate._rate_limit import Rate, RateRule
from headgate._responses import Expect, Reader, Refusal, Refused, as_bytes, event_stream, json_reader, nothing
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
    "MethodFilter",
    "Minted",
    "PathFragmentFilter",
    "QueryParamAuth",
    "Rate",
    "RateLimitError",
    "RateRule",
    "Reader",
    "Refusal",
    "Refused",
    "ResponseParseError",
    "ResponseTooLargeError",
    "StaticHeaderAuth",
    "StatusCodeFilter",
    "TransportError",
    "UpstreamStatusError",
    "as_bytes",
    "event_stream",
    "json_reader",
    "nothing",
]
