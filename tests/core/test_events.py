# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The emitter: an observer's exception is counted and swallowed, never raised into the call."""

from __future__ import annotations

from headgate._core import OBSERVER_FAILURES, SILENT, CallInfo, CallStarted, Emitter, Event
from tests.support.events import Recorder


class _Broken:
    """An observer whose ``notify`` always raises."""

    def notify(self, event: Event) -> None:
        """Raise."""
        msg = f"cannot handle {type(event).__name__}"
        raise RuntimeError(msg)


def test_a_failing_observer_is_counted_and_the_next_still_receives_the_event() -> None:
    """Count the broken observer's failure by its class and still deliver to the observers after it."""
    before = OBSERVER_FAILURES[_Broken.__qualname__]
    recorder = Recorder()
    event = CallStarted(call=CallInfo(method="GET", url="https://a.test/x"))

    Emitter((_Broken(), recorder)).emit(event)

    assert recorder.events == [event]
    assert OBSERVER_FAILURES[_Broken.__qualname__] == before + 1


def test_the_silent_emitter_has_no_listeners() -> None:
    """Report no listeners, so the client can skip building events."""
    assert not SILENT.active
    SILENT.emit(CallStarted(call=CallInfo(method="GET", url="https://a.test/x")))


def test_an_observer_that_takes_the_event_is_not_counted_as_failing() -> None:
    """Count nothing for an observer whose ``notify`` returns normally."""
    before = OBSERVER_FAILURES[Recorder.__qualname__]

    Emitter((Recorder(),)).emit(CallStarted(call=CallInfo(method="GET", url="https://a.test/x")))

    assert OBSERVER_FAILURES[Recorder.__qualname__] == before
