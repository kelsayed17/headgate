# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A timeout someone sets must fit the deadline it runs under, or it could never take effect.

Every attempt's timeouts are cut to the time the deadline leaves, so a read timeout of 120 s under a 60 s deadline is a
60 s one that says otherwise. It is refused where it is set: on the client, on a view, or on a call. A deadline set
shorter than the timeouts stays valid, since cutting them is what it is for.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import Client, HttpOptions, RequestOptions, Timeouts, TransportPolicy, nothing
from headgate._options import DEFAULT_TIMEOUTS, httpx_timeout
from headgate.testing import FakeTransport

if TYPE_CHECKING:
    from collections.abc import Callable

#: A read timeout longer than the default 60 s deadline.
_LONG = Timeouts(read=120.0)
#: The default timeouts, all within the default deadline.
_DEFAULTS = Timeouts()


def _client(*, timeout: Timeouts = _DEFAULTS, deadline: float | None = 60.0) -> Client:
    """Build a client answering every GET with 200, with *timeout* under *deadline*."""
    fake = FakeTransport().reply("GET", "/x", 200)
    return Client(
        base_url="https://api.example.test",
        policy=TransportPolicy(deadline=deadline, timeout=timeout),
        http=HttpOptions(transport=fake),
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
            lambda: _client().with_options(deadline=5.0).with_options(timeout=Timeouts(read=10.0)),
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


#: A read with no limit of its own.
_NO_READ_LIMIT = Timeouts(read=math.inf)
#: A deadline shorter than every default timeout.
_SHORT = 5.0


def test_no_limit_reaches_httpx2_as_none() -> None:
    """Hand httpx2 ``None`` for a phase set to ``math.inf``, and keep the other phases."""
    timeout = httpx_timeout(_NO_READ_LIMIT)

    assert timeout.read is None
    assert timeout.connect == DEFAULT_TIMEOUTS.connect


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: _client(timeout=_NO_READ_LIMIT, deadline=_SHORT), id="a-client"),
        pytest.param(lambda: _client(deadline=_SHORT).with_options(timeout=_NO_READ_LIMIT), id="a-view"),
    ],
)
def test_no_limit_fits_any_deadline(build: Callable[[], object]) -> None:
    """Accept no limit under a deadline: the deadline is then the limit."""
    build()


async def test_no_limit_under_a_deadline_is_cut_to_the_time_left() -> None:
    """Send a call's unlimited read with the time its deadline leaves, as an unset phase is sent."""
    seen: list[object] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx2.Response(200)

    client = Client(
        base_url="https://api.example.test",
        policy=TransportPolicy(deadline=_SHORT),
        http=HttpOptions(transport=httpx2.MockTransport(record)),
    )
    await client.get("x", expect=nothing, timeout=_NO_READ_LIMIT)

    (read,) = seen
    assert isinstance(read, float)
    assert 0 < read <= _SHORT
