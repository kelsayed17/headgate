# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The per-call context: what every layer of the stack knows about the logical call a request belongs to.

``AsyncBaseClient`` builds one :class:`CallContext` per call and stamps it on the request in a single extension.
httpx2 hands a request's extensions to every redirect hop, and the cache layer restores them below hishel, so every
layer from the auth flow down to the network transport finds the same object with :meth:`CallContext.find`. Layers
read and write it through its methods, never through extension keys of their own:

* what the call is: :attr:`~CallContext.info` (method, sanitized URL, route template, call id);
* when it must end: :attr:`~CallContext.deadline`, absolute on the client's clock, :meth:`~CallContext.remaining`,
  and :meth:`~CallContext.fit_timeouts`, which cuts a request's timeouts to it;
* its sends: :meth:`~CallContext.begin_attempt`, :attr:`~CallContext.attempt` and the current attempt's
  :attr:`~CallContext.progress` (whether a connection was obtained and how many request bytes were written);
* what it carries: :attr:`~CallContext.idempotency_key` and whether its redirects are followed;
* what happened: :meth:`~CallContext.add_note` lines, which the client adds to the error it raises, and
  :meth:`~CallContext.emit` for events;
* per-layer data: a layer that needs a value of its own on the call declares a typed :class:`Slot` in its own module and
  reads and writes it with :meth:`~CallContext.get` and :meth:`~CallContext.put`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast, override

from headgate._core._events import SILENT, CallInfo

if TYPE_CHECKING:
    import httpx2

    from headgate._core._events import Emitter, Event
    from headgate._core._time import Clock

__all__ = ["CallContext", "SendProgress", "Slot"]

#: The one request extension headgate keeps its per-call state in.
_EXTENSION: Final = "headgate.call"


@dataclass(slots=True)
class SendProgress:
    """What one attempt established about sending: whether a connection was obtained, and request bytes written.

    These are facts, recorded by whichever layer can observe them, so the retry layer can tell a request that never
    left from one that may have been processed.
    """

    connected: bool = False
    bytes_sent: int = 0

    def mark_connected(self) -> None:
        """Record that the attempt obtained a connection."""
        self.connected = True

    def add_sent(self, count: int) -> None:
        """Record that *count* more request bytes were written."""
        self.bytes_sent += count


class Slot[T]:
    """A typed key for a value one layer keeps on a :class:`CallContext`; compared by identity."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        """Name the slot, for its ``repr`` only."""
        self.name = name

    @override
    def __repr__(self) -> str:
        """Return ``Slot('name')``."""
        return f"Slot({self.name!r})"


class CallContext:
    """One logical call's state, shared by every layer that handles one of its requests. See the module docstring."""

    __slots__ = (
        "_attempt",
        "_clock",
        "_deadline",
        "_emitter",
        "_notes",
        "_progress",
        "_slots",
        "follow_redirects",
        "idempotency_key",
        "info",
        "started_at",
    )

    def __init__(
        self,
        info: CallInfo,
        *,
        clock: Clock = time.monotonic,
        deadline_after: float | None = None,
        emitter: Emitter = SILENT,
    ) -> None:
        """Start the call *info* now on *clock*, due *deadline_after* seconds from now (``None``: no deadline)."""
        self.info = info
        self._clock = clock
        self.started_at = clock()
        self._deadline = None if deadline_after is None else self.started_at + deadline_after
        self._emitter = emitter
        self._attempt = 0
        self._progress = SendProgress()
        self._notes: list[str] = []
        self._slots: dict[object, object] = {}
        #: The ``Idempotency-Key`` (or configured header) value every attempt of the call carries, if any.
        self.idempotency_key: str | None = None
        #: Whether httpx2 follows this call's redirects, read by the hook that drops a followed redirect's body.
        self.follow_redirects = True

    # Finding the context of a request.

    def attach(self, request: httpx2.Request) -> None:
        """Stamp this context on *request*."""
        request.extensions[_EXTENSION] = self

    @staticmethod
    def find(request: httpx2.Request) -> CallContext | None:
        """Return the context stamped on *request*, or ``None`` for a request no client call built."""
        found = request.extensions.get(_EXTENSION)
        return found if isinstance(found, CallContext) else None

    @staticmethod
    def of(request: httpx2.Request) -> CallContext:
        """Return the context stamped on *request*, stamping a standalone one first when it has none.

        A request a layer is handed directly (a test driving one transport, a hop an auth flow built) has no client
        call around it; its standalone context names the request's origin and path only, and has no deadline.
        """
        found = CallContext.find(request)
        if found is not None:
            return found
        bare = request.url.copy_with(query=None, fragment=None, userinfo=b"")
        context = CallContext(CallInfo(method=request.method, url=str(bare)))
        context.attach(request)
        return context

    # Time.

    @property
    def clock(self) -> Clock:
        """The clock the deadline and every duration of this call are measured on."""
        return self._clock

    @property
    def deadline(self) -> float | None:
        """The clock reading by which the call must end, or ``None`` when it has no deadline."""
        return self._deadline

    def remaining(self) -> float | None:
        """Return the seconds left before the deadline (negative once past it), or ``None`` without a deadline."""
        return None if self._deadline is None else self._deadline - self._clock()

    def elapsed(self) -> float:
        """Return the seconds since the call started."""
        return self._clock() - self.started_at

    def fit_timeouts(self, request: httpx2.Request, *, floor: float = 0.0) -> None:
        """Cut *request*'s httpx2 timeouts to the time left before the deadline, never below *floor* seconds.

        Each phase keeps its own limit when that is shorter; a phase with no limit gets the time left. A call without a
        deadline, or a request without a ``timeout`` extension, is left as it is. Every layer that waits before a send
        calls it, so the send that follows cannot outlast the call.
        """
        remaining = self.remaining()
        timeout = request.extensions.get("timeout")
        if remaining is None or not isinstance(timeout, dict):
            return
        left = max(remaining, floor)
        request.extensions["timeout"] = {
            phase: left if limit is None else min(limit, left) for phase, limit in timeout.items()
        }

    # Attempts.

    @property
    def attempt(self) -> int:
        """How many sends the call has begun: 0 before the first, then 1, 2, ..."""
        return self._attempt

    @property
    def progress(self) -> SendProgress:
        """The current attempt's send progress, fresh for each attempt."""
        return self._progress

    def begin_attempt(self) -> int:
        """Record that a send begins, reset the send progress, and return the attempt's number (from 1)."""
        self._attempt += 1
        self._progress = SendProgress()
        return self._attempt

    # Notes and events.

    @property
    def notes(self) -> tuple[str, ...]:
        """The lines layers added about the call, in order."""
        return tuple(self._notes)

    def add_note(self, note: str) -> None:
        """Add *note*, a line with no credential in it, for the error the call may raise."""
        self._notes.append(note)

    @property
    def observed(self) -> bool:
        """Whether the client has observers, so a layer can skip building an event nobody receives."""
        return self._emitter.active

    def emit(self, event: Event) -> None:
        """Deliver *event* to the client's observers."""
        self._emitter.emit(event)

    # Per-layer data.

    def get[T](self, slot: Slot[T]) -> T | None:
        """Return the value put in *slot*, or ``None``."""
        # A slot is only ever filled through put(), with a value of its own type.
        return cast("T | None", self._slots.get(slot))

    def put[T](self, slot: Slot[T], value: T) -> None:
        """Put *value* in *slot*."""
        self._slots[slot] = value
