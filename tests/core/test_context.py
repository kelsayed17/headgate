# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``CallContext``: finding a request's call, its deadline on the injected clock, attempts, notes, events and slots."""

from __future__ import annotations

import httpx2

from headgate._core import CallContext, CallInfo, CallStarted, Emitter, Event, SendProgress, Slot
from tests.support.clock import FakeClock

_URL = "https://api.example.test/v1/series"
_DEADLINE = 10.0
_STEP = 4.0


def _context(clock: FakeClock | None = None, *, deadline_after: float | None = None) -> CallContext:
    """Build a context for a GET of the test URL."""
    return CallContext(CallInfo(method="GET", url=_URL), clock=clock or FakeClock(), deadline_after=deadline_after)


def test_find_returns_the_attached_context_and_none_for_a_bare_request() -> None:
    """Find the context stamped on a request, and nothing on one no call built."""
    request = httpx2.Request("GET", _URL)
    assert CallContext.find(request) is None

    context = _context()
    context.attach(request)

    assert CallContext.find(request) is context
    assert CallContext.of(request) is context, "of() returns an existing context rather than replacing it"


def test_the_deadline_is_absolute_on_the_injected_clock() -> None:
    """Fix the deadline when the call starts, and count down on the clock the client was given."""
    clock = FakeClock(start=100.0)
    context = _context(clock, deadline_after=_DEADLINE)
    clock.advance(_STEP)

    assert context.clock is clock
    assert context.deadline == clock.now - _STEP + _DEADLINE
    assert context.remaining() == _DEADLINE - _STEP
    assert context.elapsed() == _STEP


def test_a_call_without_a_deadline_has_no_remaining_time() -> None:
    """Report no deadline and no remaining time when the call has none."""
    context = _context()
    assert context.deadline is None
    assert context.remaining() is None


def test_each_attempt_starts_with_fresh_send_progress() -> None:
    """Number attempts from one and reset the send progress for each."""
    context = _context()
    assert context.attempt == 0
    first = context.begin_attempt()
    context.progress.mark_connected()
    context.progress.add_sent(3)
    context.progress.add_sent(4)
    assert context.progress == SendProgress(connected=True, bytes_sent=7)

    second = context.begin_attempt()

    assert (first, second, context.attempt) == (1, 2, 2)
    assert context.progress == SendProgress()


def test_notes_keep_their_order_and_come_back_as_a_copy() -> None:
    """Return the notes in the order added, as a tuple the caller cannot change."""
    context = _context()
    context.add_note("attempt 1/3: 503")
    context.add_note("attempt 2/3: 200")
    assert context.notes == ("attempt 1/3: 503", "attempt 2/3: 200")


def test_a_slot_holds_one_layers_value() -> None:
    """Read back what was put in a slot, and nothing for a slot never filled."""
    filled, empty = Slot[int]("filled"), Slot[int]("empty")
    value = 3
    context = _context()
    context.put(filled, value)

    assert context.get(filled) == value
    assert context.get(empty) is None
    assert repr(filled) == "Slot('filled')"


class _Recorder:
    """An observer that keeps what it receives."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        """Keep *event*."""
        self.events.append(event)


def test_emit_delivers_to_the_clients_observers() -> None:
    """Deliver an event through the context's emitter, and say whether anyone listens."""
    recorder = _Recorder()
    info = CallInfo(method="GET", url=_URL)
    context = CallContext(info, emitter=Emitter((recorder,)))
    assert context.observed
    assert not _context().observed

    context.emit(CallStarted(call=info))

    assert recorder.events == [CallStarted(call=info)]


def test_every_call_gets_its_own_id() -> None:
    """Number calls uniquely, so an observer can tell concurrent calls apart."""
    assert CallInfo(method="GET", url=_URL).call_id != CallInfo(method="GET", url=_URL).call_id


def test_a_standalone_context_names_only_the_origin_and_path() -> None:
    """Strip the query, fragment and userinfo from a standalone context's URL, and give it no deadline."""
    request = httpx2.Request("POST", "https://user:pw@api.example.test/v1/x?token=secret#frag")

    context = CallContext.of(request)

    assert context.info.method == "POST"
    assert context.info.url == "https://api.example.test/v1/x"
    assert context.deadline is None
    assert CallContext.find(request) is context
