# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``examples/orders_client.py``: a minted token, an idempotent POST, a declared 422, pages, a stream, events."""

from __future__ import annotations

import httpx2
import pytest
from orders_client import NewOrder, Order, OrdersClient, Rejection

from headgate import AuthError
from headgate.events import CallFinished, Event
from headgate.testing import FakeTransport

_TOKEN = httpx2.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
_ORDER = {"id": "o-1", "sku": "SKU-9", "quantity": 2, "status": "open"}
_FIRST_PAGE_LINK = '<https://api.shop.example/v2/orders?status=open&per_page=100&page=2>; rel="next"'


class Recorder:
    """An observer keeping every event it is told about."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        """Keep *event*."""
        self.events.append(event)


def _shop(*routes: tuple[str, str, tuple[httpx2.Response | int, ...]]) -> FakeTransport:
    """Return a fake shop: the token endpoint, then *routes* as ``(method, path, replies)``."""
    fake = FakeTransport().route("POST", "/oauth/token", _TOKEN)
    for method, path, replies in routes:
        fake.route(method, path, *replies)
    return fake


async def test_a_token_is_minted_once_and_signs_every_call() -> None:
    """Mint one token through the client's stack, then send it as a bearer on every call."""
    fake = _shop(("GET", "/v2/orders/o-1", (httpx2.Response(200, json=_ORDER),)), ("DELETE", "/v2/orders/o-1", (204,)))
    async with OrdersClient("id", "secret", transport=fake) as shop:
        assert await shop.get_order("o-1") == Order(id="o-1", sku="SKU-9", quantity=2, status="open")
        await shop.cancel_order("o-1")

    mints = fake.calls_to("POST", "/oauth/token")
    assert len(mints) == 1
    assert mints[0].url.host == "auth.shop.example"
    assert b"grant_type=client_credentials" in mints[0].content
    assert [call.headers["Authorization"] for call in fake.calls[1:]] == ["Bearer tok-1", "Bearer tok-1"]


async def test_a_refused_mint_raises_auth_error() -> None:
    """Raise ``AuthError`` when the token endpoint refuses the client's credentials."""
    fake = FakeTransport().route("POST", "/oauth/token", 401)
    async with OrdersClient("id", "wrong", transport=fake) as shop:
        with pytest.raises(AuthError, match="token mint failed"):
            await shop.get_order("o-1")


async def test_a_post_carries_one_idempotency_key_across_its_retries() -> None:
    """Retry a POST after a 503 because it carries an idempotency key, the same key on both sends."""
    created = httpx2.Response(201, json=_ORDER)
    fake = _shop(("POST", "/v2/orders", (503, created)))
    async with OrdersClient("id", "secret", transport=fake) as shop:
        placed = await shop.place_order(NewOrder(sku="SKU-9", quantity=2))

    assert isinstance(placed, Order)
    sends = fake.calls_to("POST", "/v2/orders")
    assert [send.headers["Idempotency-Key"] for send in sends] == [sends[0].headers["Idempotency-Key"]] * 2
    assert sends[0].content == b'{"sku":"SKU-9","quantity":2}'


async def test_a_rejected_order_is_the_declared_422_answer() -> None:
    """Return the 422 body as a ``Rejection`` rather than raising."""
    fake = _shop(("POST", "/v2/orders", (httpx2.Response(422, json={"reason": "out of stock"}),)))
    async with OrdersClient("id", "secret", transport=fake) as shop:
        assert await shop.place_order(NewOrder(sku="SKU-9", quantity=99, note="rush")) == Rejection("out of stock")


async def test_orders_follow_the_link_header_and_report_events() -> None:
    """Follow ``rel="next"`` to the second page, and report one finished call per page."""
    second = {**_ORDER, "id": "o-2"}
    pages = (
        httpx2.Response(200, json=[_ORDER], headers={"Link": _FIRST_PAGE_LINK}),
        httpx2.Response(200, json=[second]),
    )
    fake = _shop(("GET", "/v2/orders", pages))
    recorder = Recorder()
    async with OrdersClient("id", "secret", transport=fake, observers=(recorder,)) as shop:
        ids = [order.id async for order in shop.orders(status="open")]

    assert ids == ["o-1", "o-2"]
    sent = fake.calls_to("GET", "/v2/orders")
    assert [dict(page.url.params) for page in sent] == [
        {"status": "open", "per_page": "100"},
        {"status": "open", "per_page": "100", "page": "2"},
    ]
    finished = [event for event in recorder.events if isinstance(event, CallFinished)]
    assert [(event.call.method, event.status_code) for event in finished] == [("GET", 200), ("GET", 200)]


async def test_the_export_streams_in_chunks() -> None:
    """Hand the export to the writer as it arrives."""
    fake = _shop(("GET", "/v2/exports/orders.csv", (httpx2.Response(200, content=b"id,sku\no-1,SKU-9\n"),)))
    chunks: list[bytes] = []
    async with OrdersClient("id", "secret", transport=fake) as shop:
        await shop.export_orders(chunks.append)

    assert b"".join(chunks) == b"id,sku\no-1,SKU-9\n"
