# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The names the README promises import from the package root."""

from __future__ import annotations

import headgate

_NAMES = [
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


def test_every_name_imports() -> None:
    """Check that the package root exports exactly the promised names, each importable."""
    for name in _NAMES:
        assert getattr(headgate, name) is not None
    assert sorted(headgate.__all__) == sorted(_NAMES)
