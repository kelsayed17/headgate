# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""An observer that keeps what it is told, and a client over a ``FakeTransport`` for the observability suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate import HttpOptions, Rate, TransportPolicy
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from headgate.events import Event, Observer
    from headgate.testing import FakeTransport

BASE = "https://api.example.test/v1"
RATE = Rate.per_second(1000)


class Recorder:
    """An observer that keeps every event it receives."""

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
    fake: FakeTransport, *observers: Observer, policy: TransportPolicy | None = None, http: HttpOptions | None = None
) -> StubClient:
    """Build a client for :data:`BASE` over *fake*, reporting to *observers*."""
    options = http or HttpOptions()
    return StubClient(
        base_url=BASE,
        rate=RATE,
        policy=policy or TransportPolicy(),
        http=HttpOptions(
            transport=fake,
            observers=(*options.observers, *observers),
            middleware=options.middleware,
            headers=options.headers,
            timeout=options.timeout,
        ),
    )
