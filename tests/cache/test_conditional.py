# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Conditional reads: the caller's validator goes out as preconditions, and a 304 comes back as ``NotModified``."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

import httpx2

from headgate import Expect, HttpOptions, TransportPolicy, as_bytes
from headgate.cache import Fresh, HttpCache, NotModified, Validator, conditional
from headgate.ratelimit import UNLIMITED
from tests.cache._helpers import every_get_policy
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage

_URL = "https://x/doc"
_ETAG = '"v2"'
_OLD_ETAG = '"v1"'
_MODIFIED = "Wed, 21 Oct 2026 07:28:00 GMT"
_OLD_MODIFIED = "Tue, 20 Oct 2026 07:28:00 GMT"


class _Origin:
    """An origin holding one representation: 304 when a precondition names it, else 200; records what it was asked."""

    def __init__(self) -> None:
        """Hold the representation ``_ETAG``, last modified at ``_MODIFIED``."""
        self.asked: list[httpx2.Headers] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer *request* per RFC 9110: If-None-Match first, else If-Modified-Since."""
        self.asked.append(request.headers)
        current = {"ETag": _ETAG, "Last-Modified": _MODIFIED}
        match = request.headers.get("If-None-Match")
        since = request.headers.get("If-Modified-Since")
        if (match is not None and match == _ETAG) or (match is None and since == _MODIFIED):
            return httpx2.Response(httpx2.codes.NOT_MODIFIED, headers={"ETag": _ETAG})
        return httpx2.Response(httpx2.codes.OK, content=b"v2 body", headers=current)


def _client(origin: _Origin, cache: HttpCache | None = None) -> StubClient:
    """Build a client over *origin*, caching through *cache* when given."""
    return StubClient(
        base_url="https://x",
        rate=UNLIMITED,
        policy=TransportPolicy(cache=cache),
        http=HttpOptions(transport=httpx2.MockTransport(origin)),
    )


async def test_a_first_read_sends_no_precondition_and_returns_the_validator() -> None:
    """Without a validator the read is unconditional and comes back fresh, carrying the validator for next time."""
    origin = _Origin()

    result = await _client(origin).get(_URL, expect=conditional(as_bytes))

    assert result == Fresh(b"v2 body", Validator(etag=_ETAG, last_modified=_MODIFIED))
    assert "If-None-Match" not in origin.asked[0]
    assert "If-Modified-Since" not in origin.asked[0]


async def test_a_current_validator_comes_back_not_modified() -> None:
    """A validator naming the current representation sends both preconditions and reads the 304 as NotModified."""
    origin = _Origin()
    held = Validator(etag=_ETAG, last_modified=_MODIFIED)

    result = await _client(origin).get(_URL, expect=conditional(as_bytes, validator=held))

    assert result == NotModified(Validator(etag=_ETAG, last_modified=_MODIFIED))
    assert origin.asked[0]["If-None-Match"] == _ETAG
    assert origin.asked[0]["If-Modified-Since"] == _MODIFIED


async def test_a_stale_validator_comes_back_fresh() -> None:
    """A validator naming an older representation gets the new one and its validator."""
    origin = _Origin()

    result = await _client(origin).get(_URL, expect=conditional(as_bytes, validator=Validator(etag=_OLD_ETAG)))

    assert result == Fresh(b"v2 body", Validator(etag=_ETAG, last_modified=_MODIFIED))


async def test_a_date_validator_alone_is_sent_as_if_modified_since() -> None:
    """A validator with only a date sends only ``If-Modified-Since``; the 304's ETag joins what the caller held."""
    origin = _Origin()

    result = await _client(origin).get(_URL, expect=conditional(as_bytes, validator=Validator(last_modified=_MODIFIED)))

    assert result == NotModified(Validator(etag=_ETAG, last_modified=_MODIFIED))
    assert "If-None-Match" not in origin.asked[0]


async def test_a_precondition_the_call_sets_itself_wins() -> None:
    """A precondition passed in ``headers=`` replaces the validator's."""
    origin = _Origin()

    result = await _client(origin).get(
        _URL, expect=conditional(as_bytes, validator=Validator(etag=_ETAG)), headers={"If-None-Match": _OLD_ETAG}
    )

    assert isinstance(result, Fresh)
    assert origin.asked[0]["If-None-Match"] == _OLD_ETAG


async def test_a_ttl_cache_answers_a_conditional_read_without_asking(storage: AsyncSqliteStorage) -> None:
    """A ``FilterPolicy`` cache never revalidates: a stored entry answers fresh and the origin is not asked again."""
    origin = _Origin()
    client = _client(origin, HttpCache(policy=every_get_policy(), storage=storage))
    first = await client.get(_URL, expect=conditional(as_bytes))
    assert isinstance(first, Fresh)

    second = await client.get(_URL, expect=conditional(as_bytes, validator=first.validator))

    assert second == first
    assert len(origin.asked) == 1


def test_validator_headers_name_each_part_it_has() -> None:
    """Each part of a validator becomes its precondition; an empty validator asks nothing."""
    assert Validator().headers() == {}
    assert Validator(etag=_ETAG, last_modified=_OLD_MODIFIED).headers() == {
        "If-None-Match": _ETAG,
        "If-Modified-Since": _OLD_MODIFIED,
    }


def test_expect_headers_are_frozen_and_survive_on() -> None:
    """``Expect.headers`` is a read-only copy, carried by every ``Expect`` that ``on`` widens it into."""
    headers = {"If-None-Match": _ETAG}
    expect = Expect(as_bytes, headers=headers).on(httpx2.codes.NOT_MODIFIED, as_bytes)
    headers["If-None-Match"] = _OLD_ETAG

    assert dict(expect.headers) == {"If-None-Match": _ETAG}
    assert isinstance(expect.headers, MappingProxyType)
