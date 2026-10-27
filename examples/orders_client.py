# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A fuller client: a minted OAuth token, an idempotent POST, a declared business refusal, pages and a stream.

The shop's API takes a bearer token minted by OAuth 2.0 client credentials, documents 422 as the answer to an order it
rejects, links its pages with a ``Link`` header, and serves large exports as CSV.
``tests/examples/test_orders_client.py`` runs every method against a fake network.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, override

import httpx2
import msgspec

from headgate import (
    AsyncBaseClient,
    CachedTokenAuth,
    Expect,
    HttpOptions,
    IdempotencyKeys,
    LinkHeader,
    Minted,
    Rate,
    RateRule,
    RetryPolicy,
    Route,
    Timeouts,
    TransportPolicy,
    nothing,
)
from headgate.msgspec import decode, decoder, encoder

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from headgate import Paginated
    from headgate.events import Observer

#: Where tokens are minted: another origin than the API's, trusted for the mint's own hops only.
MINT_ENDPOINT = "https://auth.shop.example/oauth/token"


class Token(msgspec.Struct, frozen=True):
    """An OAuth 2.0 token response."""

    access_token: str
    expires_in: float


class Order(msgspec.Struct, frozen=True):
    """An order as the API returns it."""

    id: str
    sku: str
    quantity: int
    status: str


class NewOrder(msgspec.Struct, frozen=True, omit_defaults=True):
    """An order to place; ``note`` is left out of the body when it is not set."""

    sku: str
    quantity: int
    note: str | None = None


class Rejection(msgspec.Struct, frozen=True):
    """Why the API rejected an order: its documented 422 answer."""

    reason: str


class ShopTokenAuth(CachedTokenAuth):
    """OAuth 2.0 client credentials, minted through the client's own stack and cached until shortly before expiry."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        """Mint with *client_id* and *client_secret*."""
        super().__init__()
        self._form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}

    @override
    def credential_identity(self) -> bytes:
        """Name the long-lived credential, so two instances holding it share cache entries; it is hashed, never kept."""
        return f"{self._form['client_id']}\0{self._form['client_secret']}".encode()

    @override
    async def mint_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        del request  # the hop needs nothing of the call: it is sent with the call's timeouts already
        # The client's stack paces the hop, retries it when safe, maps its failures, and hands the response back read.
        response = yield httpx2.Request("POST", MINT_ENDPOINT, data=self._form)
        response.raise_for_status()  # a refused mint raises AuthError, naming the token endpoint
        token = decode(response.content, Token)
        yield Minted(token.access_token, token.expires_in)


def orders_on(page: list[Order]) -> list[Order]:
    """Return the orders on *page*: the body is the list itself."""
    return page


ORDER = decoder(Order)
NEW_ORDER = encoder(NewOrder)
ORDER_OR_REJECTION = Expect(ORDER).on(HTTPStatus.UNPROCESSABLE_ENTITY, decoder(Rejection))
ORDER_PAGES = LinkHeader(items=orders_on)


class OrdersClient(AsyncBaseClient):
    """The shop's order API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        observers: tuple[Observer, ...] = (),
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """Mint tokens with *client_id* and *client_secret*; report to *observers*; *transport* replaces the network."""
        super().__init__(
            base_url="https://api.shop.example/v2",
            rate=Rate.per_second(20),
            auth=ShopTokenAuth(client_id, client_secret),
            policy=TransportPolicy(
                retry=RetryPolicy(attempts=4),  # four sends at most, the first included
                deadline=30.0,  # each call, every attempt and wait included
                idempotency=IdempotencyKeys(),  # every POST and PATCH carries a key, so it is retried safely
                rate_limits=(RateRule("/exports", Rate.per_minute(6)),),  # a slower lane for the export endpoint
                routes=(Route("/exports", deadline=300.0),),  # and a longer deadline
            ),
            http=HttpOptions(
                transport=transport,
                timeout=Timeouts(read=20.0),
                user_agent="shop-sync/1.4",
                observers=observers,
            ),
        )

    async def get_order(self, order_id: str) -> Order:
        """Return the order *order_id*."""
        return await self.get(t"orders/{order_id}", expect=ORDER)

    async def place_order(self, order: NewOrder) -> Order | Rejection:
        """Place *order*: the order as created, or the API's documented rejection."""
        return await self.post("orders", body=NEW_ORDER(order), expect=ORDER_OR_REJECTION)

    async def cancel_order(self, order_id: str) -> None:
        """Cancel the order *order_id*."""
        await self.delete(t"orders/{order_id}", expect=nothing)

    def orders(self, *, status: str) -> Paginated[list[Order], Order]:
        """Return the orders in *status*, a page of 100 at a time."""
        query = {"status": status, "per_page": 100}
        return self.paginate("orders", expect=decoder(list[Order]), pages=ORDER_PAGES, params=query)

    async def export_orders(self, write: Callable[[bytes], object]) -> None:
        """Stream the CSV export of every order into *write*, a chunk at a time, never holding it whole."""
        async with self.stream("exports/orders.csv") as export:
            async for chunk in export.aiter_bytes():
                write(chunk)
