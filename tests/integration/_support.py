# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What the integration suite shares: an observer that keeps every event, and a client over a test network."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, override

import msgspec

from headgate import CachedTokenAuth, HttpOptions, QueryParamAuth, Rate, StaticHeaderAuth, TransportPolicy
from headgate._core import CredentialStamper
from tests.support.clients import StubClient

if TYPE_CHECKING:
    import httpx2

    from headgate import RateBudget
    from headgate.events import Event

#: The origin every client here is built for, and its path prefix.
BASE = "https://api.example.test/v1"

#: A rate no test here waits on unless it is about the rate.
FAST = Rate.per_second(1000)


class Recorder:
    """An observer that keeps every event it receives, in order."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        """Keep *event*."""
        self.events.append(event)

    def of[E](self, kind: type[E]) -> list[E]:
        """Return the events of *kind*, in order."""
        return [event for event in self.events if isinstance(event, kind)]


def client(
    transport: httpx2.AsyncBaseTransport,
    *,
    policy: TransportPolicy | None = None,
    http: HttpOptions | None = None,
    auth: httpx2.Auth | None = None,
    rate: Rate | RateBudget = FAST,
) -> StubClient:
    """Build a client for :data:`BASE` over *transport*, spending *rate*, with *http*'s other options."""
    options = http or HttpOptions()
    return StubClient(
        base_url=BASE,
        rate=rate,
        auth=auth,
        policy=policy or TransportPolicy(),
        http=HttpOptions(
            transport=transport,
            observers=options.observers,
            middleware=options.middleware,
            headers=options.headers,
            timeout=options.timeout,
            trusted_origins=options.trusted_origins,
            allow_private_hosts=options.allow_private_hosts,
            resolver=options.resolver,
            follow_redirects=options.follow_redirects,
        ),
    )


class Page(msgspec.Struct, frozen=True):
    """A page of item ids, naming the next page's URL."""

    data: list[int]
    next: str | None = None


def items_of(page: Page) -> list[int]:
    """Return the items on *page*."""
    return page.data


def next_of(page: Page) -> str | None:
    """Return the URL of the page after *page*."""
    return page.next


class Bearer(CachedTokenAuth):
    """A bearer auth whose exchange never runs: :func:`seeded` caches its token."""

    @override
    async def _mint_token(self) -> tuple[str, float]:
        raise NotImplementedError


#: The token :func:`seeded` caches.
BEARER = "minted-bearer"


def seeded() -> Bearer:
    """Return a :class:`Bearer` holding :data:`BEARER`, never expiring, so it stamps without minting."""
    auth = Bearer()
    auth.seed(BEARER, math.inf)
    return auth


def real_auths() -> dict[str, httpx2.Auth]:
    """Return one of each auth headgate ships, by name, each ready to stamp without a network exchange."""
    return {
        "static-header": StaticHeaderAuth({"X-Api-Key": "header-secret"}),
        "query-param": QueryParamAuth({"api_key": "param-secret"}),
        "cached-token": seeded(),
    }


def carried(request: httpx2.Request, auth: httpx2.Auth) -> set[str]:
    """Return the names of the credentials *auth* declares that *request* carries."""
    assert isinstance(auth, CredentialStamper)
    headers = {name for name in auth.credential_headers if name in request.headers}
    return headers | {name for name in auth.credential_params if name in request.url.params}
