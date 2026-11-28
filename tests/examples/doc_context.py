# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What the guides' code samples take as given: the domain types, values and SDKs a sample names but does not define.

``test_doc_samples.py`` puts every marked sample of a page after ``from headgate import *`` and
``from tests.examples.doc_context import *``, so a sample reads like the guide's prose around it: an ``Order`` is an
order, ``client`` a client, ``api_key`` a key. Each name here is real, so a sample marked to run runs on it; the ones a
sample only type-checks against (``client``, ``catalog``) are clients that send nothing until called.
"""

import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Self, Unpack

import httpx2
import msgspec
from hishel import AsyncBaseStorage, AsyncSqliteStorage
from quickstart import CatalogClient, Series

from headgate import (
    CallOptions,
    CallPolicy,
    CallUrl,
    Client,
    HttpCache,
    Rate,
    Route,
    StaticHeaderAuth,
    as_bytes,
    nothing,
)
from headgate.msgspec import reader
from headgate.pagination import Cursor
from headgate.testing import FakeTransport, fake_network

__all__ = [
    "GDP",
    "ORDER",
    "ORDER_PAGES",
    "REPORT",
    "SERIES",
    "ArchiveClient",
    "AsyncIterator",
    "AsyncSqliteStorage",
    "CallOptions",
    "CatalogClient",
    "ExchangeClient",
    "FilingsPage",
    "HTTPStatus",
    "Issue",
    "Item",
    "NewOrder",
    "Order",
    "Page",
    "Path",
    "Query",
    "Rejection",
    "Report",
    "Row",
    "SearchPage",
    "Self",
    "Series",
    "Statement",
    "Unpack",
    "api_key",
    "blob_id",
    "catalog",
    "client",
    "csv_bytes",
    "decode_allotments",
    "decode_statements",
    "handle",
    "hits_of",
    "httpx2",
    "msgspec",
    "order",
    "partner_auth",
    "path",
    "reader_or_expect",
    "record_call",
    "record_wait",
    "report",
    "report_id",
    "reschedule",
    "results_of",
    "rows_of",
    "sink",
    "symbol",
    "token",
    "url",
]


class Order(msgspec.Struct, frozen=True):
    """An order, as the API answers with one."""

    id: str
    qty: int


class NewOrder(msgspec.Struct, frozen=True):
    """An order to place."""

    sku: str
    qty: int


class Rejection(msgspec.Struct, frozen=True):
    """Why the API refused an order."""

    reason: str


class Report(msgspec.Struct, frozen=True):
    """A report that takes long to build."""

    id: str


class Item(msgspec.Struct, frozen=True):
    """One item on a page."""

    id: int


class Page(msgspec.Struct, frozen=True):
    """A page of items, naming the cursor of the next."""

    data: list[Item]
    next_cursor: str | None = None


class Issue(msgspec.Struct, frozen=True):
    """A GitHub issue."""

    number: int
    title: str


class Total(msgspec.Struct, frozen=True):
    """A search's total: exact (``eq``) or a lower bound (``gte``)."""

    value: int
    relation: str


class SearchPage(msgspec.Struct, frozen=True):
    """A page of search hits, with the total the search found."""

    hits: list[Item]
    total: Total


class FilingsPage(msgspec.Struct, frozen=True):
    """A page of filings, naming the next page when there is one."""

    results: list[Item]
    next: str | None = None


class Query(msgspec.Struct, frozen=True):
    """A search posted as a request body, carrying its cursor or offset."""

    kind: str
    limit: int
    cursor: str | None = None
    offset: int = 0


@dataclass(frozen=True)
class Row:
    """A record with one field, ``value: int``: what ``check_codec(record=)`` asks for."""

    value: int


@dataclass(frozen=True)
class Statement:
    """A row of a fixed-width statement."""

    line: str


def hits_of(page: SearchPage) -> list[Item]:
    """Return a search page's hits."""
    return page.hits


def results_of(page: FilingsPage) -> list[Item]:
    """Return a filings page's results."""
    return page.results


def rows_of(page: list[Row]) -> list[Row]:
    """Return the rows of a page that is a list of rows."""
    return page


def decode_allotments(content: bytes) -> list[str]:
    """Return the lines of a body of allotments."""
    return content.decode().splitlines()


def decode_statements(content: bytes, *, table: type[Statement]) -> list[Statement]:
    """Return a statement's lines as rows of *table*."""
    return [table(line) for line in content.decode().splitlines()]


def record_call(method: str, route: str, status: int | None, error: str | None, elapsed: float) -> None:
    """Record a call in a metrics backend."""
    del method, route, status, error, elapsed


def record_wait(reason: str, delay: float) -> None:
    """Record a rate-limit wait in a metrics backend."""
    del reason, delay


def report(title: str | None, detail: str | None) -> None:
    """Tell an operator about a problem the provider described."""
    del title, detail


def reschedule(*, after: float | None) -> None:
    """Run the job again later."""
    del after


def handle(event: str, data: str) -> None:
    """Handle one server-sent event."""
    del event, data


def _last_page(page: list[Order]) -> str | None:
    """Return no cursor: every page of orders is the last."""
    del page
    return None


class ExchangeClient:
    """An exchange's SDK: ten requests a second, and a lane of its own for candles."""

    def __init__(self, api_key: str) -> None:
        """Hold a client signed with *api_key*."""
        self._http = Client(
            base_url="https://api.exchange.example",
            policy=CallPolicy(rate=Rate.per_second(10), routes=(Route("/candles", rate=Rate.per_second(1)),)),
            auth=StaticHeaderAuth({"X-Api-Key": api_key}),
        )


class ArchiveClient:
    """An archive's SDK: its archives cached for a day, its status never."""

    def __init__(self, cache_storage: AsyncBaseStorage) -> None:
        """Hold a client caching the archives in *cache_storage*."""
        cache = HttpCache(storage=cache_storage, paths=("/archives",), ttl=86_400.0)
        self._http = Client(base_url="https://archive.example", policy=CallPolicy(cache=cache))

    async def __aenter__(self) -> Self:
        """Return the SDK."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the client."""
        await self._http.aclose()

    async def archive(self, name: str) -> bytes:
        """Return an archive."""
        return await self._http.get(t"archives/{name}", expect=as_bytes)

    async def status(self) -> None:
        """Ask for the service's status."""
        await self._http.get("status", expect=nothing)


#: The domain values the samples read and send.
GDP = {"id": "GDP", "title": "Gross domestic product", "values": [27.4, 28.1]}
SERIES = reader(Series)
ORDER = reader(Order)
REPORT = reader(Report)
ORDER_PAGES = Cursor(reader(list[Order]), items=list, next=_last_page, param="cursor")
api_key = "test-key"
token = api_key
blob_id = "b-1"
report_id = "r-1"
symbol = "BTC/USD"
csv_bytes = b"a,b\n1,2\n"
path = Path("q3.csv")
order = NewOrder(sku="A-1", qty=2)
url: CallUrl = "series/GDP"
reader_or_expect = SERIES
partner_auth = StaticHeaderAuth({"X-Partner-Key": "test-key"})
sink = io.BytesIO()

#: Clients a sample calls, on a fake network that answers nothing: the samples that call them only type-check, and a
#: call that ran anyway would raise rather than reach the internet.
with fake_network(FakeTransport()):
    client = Client(base_url="https://api.example.com/v1")
    catalog = CatalogClient(api_key)
