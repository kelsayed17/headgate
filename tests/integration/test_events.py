# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every layer reports through the call's context: one client's calls emit every event type there is.

``CallFinished`` reports the final status of a call that raised on it, a 429 or 5xx that outlasted its retries
included, so an observer (the OpenTelemetry one among them) needs nothing but that event to name the outcome.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, get_args

import httpx2
import pytest

from headgate import (
    BreakerPolicy,
    ClientError,
    HttpCache,
    HttpOptions,
    RateLimitError,
    TransportPolicy,
    UpstreamStatusError,
    nothing,
)
from headgate.events import CallFinished, Event
from headgate.testing import FakeTransport, isolated_breakers
from tests.cache._helpers import every_get_policy
from tests.integration._support import client
from tests.support.events import Recorder

if TYPE_CHECKING:
    from hishel import AsyncSqliteStorage

    from tests.support.clients import StubClient

_DEPRECATED = {"Deprecation": "@1688169599", "Cache-Control": "max-age=60"}
_LIMITED = httpx2.Response(429, headers={"Retry-After": "1"})


def _every_layer(storage: AsyncSqliteStorage, recorder: Recorder) -> StubClient:
    """Build a client with every reporting layer: retries, a cache, a breaker that trips at once, observers."""
    fake = (
        FakeTransport()
        .reply("GET", "/v1/cached", httpx2.Response(200, headers=_DEPRECATED))
        .reply("GET", "/v1/flaky", 503, 200)
        .reply("GET", "/v1/limited", _LIMITED)
        .reply("GET", "/v1/down", 503)
    )
    policy = TransportPolicy(
        cache=HttpCache(policy=every_get_policy(), storage=storage), breaker=BreakerPolicy(failure_threshold=1)
    )
    with isolated_breakers():
        return client(fake, policy=policy, http=HttpOptions(observers=(recorder,)))


async def test_one_clients_calls_emit_every_event_type(storage: AsyncSqliteStorage) -> None:
    """Emit each type of :data:`~headgate.events.Event` from a cached, a retried, a limited and a failing call."""
    recorder = Recorder()
    built = _every_layer(storage, recorder)

    for path in ("cached", "flaky", "limited", "down"):
        with contextlib.suppress(ClientError):
            await built.get(path, expect=nothing)

    assert {type(event) for event in recorder.events} == set(get_args(Event.__value__))


@pytest.mark.parametrize(
    ("path", "error", "status"), [("limited", RateLimitError, 429), ("down", UpstreamStatusError, 503)]
)
async def test_call_finished_reports_the_status_a_call_raised_on(
    path: str, error: type[ClientError], status: int, storage: AsyncSqliteStorage
) -> None:
    """Report the surviving 429 or 5xx as the finished call's status, beside the error it raised."""
    recorder = Recorder()
    built = _every_layer(storage, recorder)

    with pytest.raises(error):
        await built.get(path, expect=nothing)

    (finished,) = recorder.of(CallFinished)
    assert (finished.status_code, finished.error) == (status, error.__name__)
