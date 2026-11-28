# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The walk, not the pager, resolves a next-page URL and refuses an https-to-http step, for every pager.

A custom pager returns ``PageRequest(httpx2.URL(target))`` for a URL it read off the wire, as the docs say. The walk
resolves it against the URL that answered (after any redirect), so a relative one does not resolve against
``base_url``, and refuses a next page that would take an ``https`` walk down to plain ``http``, before it is sent.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx2
import pytest

from headgate import BearerAuth, Client, HttpOptions, Reader
from headgate.errors import ForbiddenDestinationError
from headgate.pagination import PageRequest

type _Page = dict[str, list[int]]

_INTS: Reader[_Page] = Reader(lambda response: response.json(), name="ints")


@dataclass(frozen=True)
class _HeaderUrl:
    """A pager whose next URL is the ``X-Next-Url`` header, absolute or relative, forwarded as the docs say."""

    def first(self, request: PageRequest) -> PageRequest:
        return request

    def page_items(self, page: _Page) -> list[int]:
        return page["items"]

    def next_page(self, page: _Page, request: PageRequest, response: httpx2.Response) -> PageRequest | None:
        del page, request
        target = response.headers.get("X-Next-Url")
        return None if target is None else PageRequest(httpx2.URL(target))


def _client(answers: dict[str, httpx2.Response], sent: list[httpx2.Request]) -> Client:
    """Return a client on ``https://api.example`` answering each path from *answers*, recording what it sends."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return answers[request.url.path]

    return Client(
        base_url="https://api.example/v1",
        auth=BearerAuth("tok"),
        http=HttpOptions(transport=httpx2.MockTransport(handle)),
    )


async def test_a_custom_pager_cannot_downgrade_the_walk() -> None:
    """Refuse a custom pager's next page on plain ``http``, sending nothing to it."""
    sent: list[httpx2.Request] = []
    answers = {
        "/v1/things": httpx2.Response(
            200, json={"items": [1]}, headers={"X-Next-Url": "http://api.example/things2?cursor=secret"}
        ),
        "/things2": httpx2.Response(200, json={"items": [2]}),
    }
    async with _client(answers, sent) as client:
        with pytest.raises(ForbiddenDestinationError, match=r"from https to http://api\.example/things2"):
            await client.paginate("things", expect=_INTS, pages=_HeaderUrl()).collect()

    assert [str(request.url) for request in sent] == ["https://api.example/v1/things"]


async def test_a_custom_pager_relative_url_resolves_against_the_answering_url() -> None:
    """Resolve a relative next URL against the URL that answered the page, after its redirect, not ``base_url``."""
    sent: list[httpx2.Request] = []
    answers = {
        "/v1/things": httpx2.Response(302, headers={"Location": "https://api.example/v2/things"}),
        "/v2/things": httpx2.Response(200, json={"items": [1]}, headers={"X-Next-Url": "page2"}),
        "/v2/page2": httpx2.Response(200, json={"items": [2]}),
    }
    async with _client(answers, sent) as client:
        items = await client.paginate("things", expect=_INTS, pages=_HeaderUrl()).collect()

    assert items == [1, 2]
    assert str(sent[-1].url) == "https://api.example/v2/page2"
