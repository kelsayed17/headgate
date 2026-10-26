# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A timeout someone sets must fit the deadline it runs under, or it could never take effect.

Every attempt's timeouts are cut to the time the deadline leaves, so a read timeout of 120 s under a 60 s deadline is a
60 s one that says otherwise. It is refused where it is set: on the client, on a view, or on a call. A deadline set
shorter than the timeouts stays valid, since cutting them is what it is for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from headgate import HttpOptions, RequestOptions, Timeouts, TransportPolicy, nothing
from headgate.ratelimit import UNLIMITED
from headgate.testing import FakeTransport
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import Callable

#: Longer than the default 60 s deadline.
_LONG = 120.0
#: The default timeouts, all within the default deadline.
_DEFAULTS = Timeouts()


def _client(*, timeout: Timeouts | float = _DEFAULTS, deadline: float | None = 60.0) -> StubClient:
    """Build a client answering every GET with 200, with *timeout* under *deadline*."""
    fake = FakeTransport().route("GET", "/x", 200)
    return StubClient(
        base_url="https://api.example.test",
        rate=UNLIMITED,
        policy=TransportPolicy(deadline=deadline),
        http=HttpOptions(transport=fake, timeout=timeout),
    )


@pytest.mark.parametrize(
    ("build", "message"),
    [
        pytest.param(lambda: _client(timeout=_LONG), "read timeout 120s is longer than the 60s deadline", id="read"),
        pytest.param(
            lambda: _client(timeout=Timeouts(read=5.0, pool=90.0)), "pool timeout 90s is longer", id="a-set-phase"
        ),
        pytest.param(lambda: _client().with_options(timeout=_LONG), "read timeout 120s", id="a-view"),
        pytest.param(
            lambda: _client().with_options(deadline=5.0).with_options(timeout=10.0),
            "read timeout 10s is longer than the 5s deadline",
            id="a-view-over-a-view",
        ),
    ],
)
def test_a_timeout_longer_than_its_deadline_is_refused_where_it_is_set(
    build: Callable[[], object], message: str
) -> None:
    """Refuse the timeout, naming the phase, both durations and how to fix it."""
    with pytest.raises(ValueError, match=message) as raised:
        build()
    assert "raise the deadline" in str(raised.value)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: _client(deadline=10.0), id="a-short-deadline-over-the-default-timeouts"),
        pytest.param(lambda: _client(timeout=_LONG, deadline=None), id="no-deadline"),
        pytest.param(lambda: _client(timeout=_LONG, deadline=200.0), id="a-longer-deadline"),
        pytest.param(lambda: _client().with_options(timeout=_LONG, deadline=200.0), id="a-view-raising-both"),
        pytest.param(lambda: _client().with_options(deadline=None).with_options(timeout=_LONG), id="a-view-off"),
        pytest.param(lambda: _client().with_options(deadline=5.0), id="a-view-shortening-the-deadline"),
    ],
)
def test_a_timeout_that_fits_or_a_shorter_deadline_is_accepted(build: Callable[[], object]) -> None:
    """Accept a timeout within its deadline, any timeout without one, and a deadline that cuts the timeouts."""
    build()


async def test_a_calls_timeout_longer_than_its_deadline_is_refused() -> None:
    """Refuse a call's ``timeout=`` that outlasts the deadline the call runs under, before anything is sent."""
    client = _client()
    with pytest.raises(ValueError, match="read timeout 120s is longer than the 60s deadline"):
        await client.get("x", expect=nothing, timeout=_LONG)


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"timeout": _LONG, "deadline": None}, id="with-no-deadline"),
        pytest.param({"timeout": _LONG, "deadline": 200.0}, id="with-a-longer-deadline"),
        pytest.param({"deadline": 1.0}, id="a-short-deadline-alone"),
    ],
)
async def test_a_calls_timeout_that_fits_is_sent(options: RequestOptions) -> None:
    """Send a call whose timeout fits its own deadline, and one that only shortens the deadline."""
    client = _client()
    await client.get("x", expect=nothing, **options)
