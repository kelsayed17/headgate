# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A call's own ``max_response_bytes`` raises or lowers the client's cap for that call alone.

It applies at both layers that hold a body: the client's read, and a cached client's buffering.

The network answers 3 KiB, streamed, so each layer has to count it rather than find it already in memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest

from headgate import HttpOptions, as_bytes
from headgate.cache import HttpCache
from headgate.errors import ResponseTooLargeError
from tests.cache._helpers import TrackingNetwork, every_get_rules, stub_client

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage

    from tests.support.clients import StubClient

_KIB: Final = 1024
#: The body every request is answered with: past one cap, inside the other.
_BODY: Final = (b"x" * _KIB,) * 3
_SMALL: Final = _KIB
_LARGE: Final = 4 * _KIB


def _client(network: TrackingNetwork, *, cap: int, storage: AsyncSqliteStorage | None) -> StubClient:
    """Return a client capped at *cap* over *network*, caching every GET into *storage* when given."""
    cache = None if storage is None else HttpCache(**every_get_rules(), storage=storage)
    return stub_client(transport=network, cache=cache, http=HttpOptions(max_response_bytes=cap))


@pytest.mark.parametrize("cached", [False, True], ids=["uncached", "cached"])
async def test_a_call_raises_the_cap_for_itself(*, cached: bool, storage: AsyncSqliteStorage) -> None:
    """Read a body past the client's cap when the call allows it, and still refuse it to a call that does not."""
    network = TrackingNetwork(_BODY)
    client = _client(network, cap=_SMALL, storage=storage if cached else None)

    assert await client.get("file", expect=as_bytes, max_response_bytes=_LARGE) == b"".join(_BODY)
    with pytest.raises(ResponseTooLargeError):
        await client.get("file", expect=as_bytes)


@pytest.mark.parametrize("cached", [False, True], ids=["uncached", "cached"])
async def test_a_call_lowers_the_cap_for_itself(*, cached: bool, storage: AsyncSqliteStorage) -> None:
    """Refuse a body inside the client's cap when the call allows less, and read it on a call that does not."""
    network = TrackingNetwork(_BODY)
    client = _client(network, cap=_LARGE, storage=storage if cached else None)

    with pytest.raises(ResponseTooLargeError):
        await client.get("file", expect=as_bytes, max_response_bytes=_SMALL)
    assert await client.get("file", expect=as_bytes) == b"".join(_BODY)
    # The refused body was never stored, so the second call went to the network.
    assert len(network.urls) == len(("refused", "read"))


async def test_a_cached_client_stores_up_to_the_calls_cap(storage: AsyncSqliteStorage) -> None:
    """Store a body past the client's cap for the call that allowed it, and check every read against its own cap.

    So the entry is served to that call again from storage, and refused to a call under the client's cap, as the same
    body from the network would be.
    """
    network = TrackingNetwork(_BODY)
    client = _client(network, cap=_SMALL, storage=storage)

    await client.get("file", expect=as_bytes, max_response_bytes=_LARGE)
    assert await client.get("file", expect=as_bytes, max_response_bytes=_LARGE) == b"".join(_BODY)
    with pytest.raises(ResponseTooLargeError):
        await client.get("file", expect=as_bytes)

    assert len(network.urls) == 1


@pytest.mark.parametrize(
    ("cap", "error", "message"),
    [
        (0, ValueError, r"^max_response_bytes must be > 0 \(got 0\)$"),
        (-1, ValueError, r"^max_response_bytes must be > 0 \(got -1\)$"),
        (True, TypeError, r"^max_response_bytes= takes a number of bytes, an int \(got bool\)$"),
        (1.5, TypeError, r"^max_response_bytes= takes a number of bytes, an int \(got float\)$"),
    ],
    ids=["zero", "negative", "bool", "float"],
)
async def test_a_bad_cap_is_refused(cap: object, error: type[Exception], message: str) -> None:
    """Refuse a cap no body could fit, or one that is not a whole number of bytes, before sending."""
    network = TrackingNetwork(_BODY)
    options: dict[str, Any] = {"max_response_bytes": cap}

    with pytest.raises(error, match=message):
        await _client(network, cap=_LARGE, storage=None).get("file", expect=as_bytes, **options)

    assert network.urls == []


def test_a_stream_takes_no_cap() -> None:
    """Refuse a cap on ``stream()``, whose body is never buffered, so a cap there would do nothing."""
    client = _client(TrackingNetwork(_BODY), cap=_LARGE, storage=None)
    options: dict[str, Any] = {"max_response_bytes": _LARGE}

    with pytest.raises(TypeError, match=r"^stream\(\) got an unexpected keyword argument 'max_response_bytes'$"):
        client.stream("file", **options)
