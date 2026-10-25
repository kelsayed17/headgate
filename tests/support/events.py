# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""An observer for tests: it keeps every event a client delivers, in order."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from headgate.events import Event


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
