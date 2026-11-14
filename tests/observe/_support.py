# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""An observer that keeps what it is told, and a client over a ``FakeTransport`` for the observability suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate import Client, HttpOptions, Rate, TransportPolicy

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx2

    from headgate import Middleware, RateBudget
    from headgate.events import Observer
    from headgate.testing import FakeTransport

BASE = "https://api.example.test/v1"
RATE = Rate.per_second(1000)


def client(
    fake: FakeTransport,
    *observers: Observer,
    policy: TransportPolicy | None = None,
    http: HttpOptions | None = None,
    auth: httpx2.Auth | None = None,
    rate: Rate | RateBudget = RATE,
    middleware: Sequence[Middleware] = (),
) -> Client:
    """Build a client for :data:`BASE` over *fake*, spending *rate*, reporting to *observers*, with *http*'s options."""
    options = http or HttpOptions()
    return Client(
        base_url=BASE,
        rate=rate,
        auth=auth,
        policy=policy or TransportPolicy(),
        http=HttpOptions(transport=fake, headers=options.headers),
        observers=observers,
        middleware=middleware,
    )
