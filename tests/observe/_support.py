# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""An observer that keeps what it is told, and a client over a ``FakeTransport`` for the observability suite."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from headgate import Auth, CallPolicy, Client, HttpOptions, Rate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from headgate import Middleware, RateBudget
    from headgate.events import Observer
    from headgate.testing import FakeTransport

BASE = "https://api.example.test/v1"
RATE = Rate.per_second(1000)


def client(
    fake: FakeTransport,
    *observers: Observer,
    policy: CallPolicy | None = None,
    http: HttpOptions | None = None,
    auth: Auth | None = None,
    rate: Rate | RateBudget = RATE,
    middleware: Sequence[Middleware] = (),
) -> Client:
    """Build a client for :data:`BASE` over *fake*, spending *rate*, reporting to *observers*, with *http*'s options."""
    options = http or HttpOptions()
    return Client(
        base_url=BASE,
        auth=auth,
        policy=dataclasses.replace(policy or CallPolicy(), rate=rate),
        http=HttpOptions(transport=fake, headers=options.headers),
        observers=observers,
        middleware=middleware,
    )
