"""The names the README promises import from the package root."""

from __future__ import annotations

import headgate

_NAMES = [
    "AsyncBaseClient", "Rate", "Expect", "Refused", "nothing", "ClientError", "TransportError",
    "ClientTimeoutError", "UpstreamStatusError", "RateLimitError", "AuthError", "ResponseParseError",
    "StaticHeaderAuth", "QueryParamAuth", "CachedTokenAuth", "HttpCache", "Minted", "MethodFilter",
    "PathFragmentFilter", "StatusCodeFilter", "Reader", "Refusal", "as_bytes", "event_stream", "json_reader",
    "RateRule", "ClientConfigurationError", "DecodeError", "ResponseTooLargeError",
]  # fmt: skip


def test_every_name_imports() -> None:
    for name in _NAMES:
        assert name in headgate.__all__
        assert getattr(headgate, name) is not None
    assert sorted(headgate.__all__) == sorted(_NAMES)
