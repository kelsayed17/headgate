# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Retries through a whole client: what the raised error records, and how retries meet the breaker and the budget."""

from __future__ import annotations

import json

import pytest

from headgate import (
    HttpOptions,
    IdempotencyKeys,
    RateLimitError,
    RetryPolicy,
    TransportPolicy,
    UpstreamStatusError,
    json_reader,
    nothing,
)
from headgate._core import RetryScheduled, Timing, timing_override
from headgate.errors import TransportError
from headgate.ratelimit import Rate
from headgate.retry import Backoff, RetryBudget
from headgate.testing import ConnectFail, DropMidBody, FaultyTransport, Respond, Slow, TestClock
from tests.support.breakers import failures
from tests.support.clients import StubClient

#: The host every test client addresses.
_HOST = "api.test"

#: A backoff that always waits its full nominal delay.
_FULL = Backoff(jitter=lambda: 1.0)

#: A ``Retry-After`` longer than the default ``max_retry_after``.
_TOO_LONG = 100.0

#: The three attempts of the default policy.
_ATTEMPTS = 3


class _Events:
    """An observer that keeps every event."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[object] = []

    def notify(self, event: object) -> None:
        """Keep *event*."""
        self.events.append(event)


def _client(
    network: FaultyTransport,
    retry: RetryPolicy | None = None,
    *,
    idempotency: IdempotencyKeys | None = None,
    deadline: float | None = None,
) -> StubClient:
    """Build a client over *network* with *retry* (full backoff, no budget, by default), *idempotency*, *deadline*."""
    chosen = RetryPolicy(backoff=_FULL, budget=None) if retry is None else retry
    return StubClient(
        base_url=f"https://{_HOST}",
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=chosen, idempotency=idempotency, deadline=deadline),
        http=HttpOptions(transport=network),
    )


async def test_an_exhausted_call_records_its_attempts_and_history() -> None:
    """Raise the last 503 with the attempt count, marked retryable, and one history line per attempt."""
    network = FaultyTransport(schedule=[Respond(503), ConnectFail(), Respond(503)])

    with pytest.raises(UpstreamStatusError) as raised:
        await _client(network).get("series", expect=nothing)

    error = raised.value
    assert (error.status_code, error.attempts, error.retryable) == (503, _ATTEMPTS, True)
    history = [note for note in error.__notes__ if note.startswith("attempt ")]
    assert history == [
        "attempt 1/3: 503 Service Unavailable after 0.0s",
        "attempt 2/3: ConnectError after 0.0s",
        "attempt 3/3: 503 Service Unavailable after 0.0s",
    ]


async def test_a_429_asking_too_long_raises_at_once_with_its_retry_after() -> None:
    """Raise ``RateLimitError`` after one attempt, carrying the delay the server asked for."""
    network = FaultyTransport(schedule=[Respond(429, retry_after=_TOO_LONG), Respond(200)])

    with pytest.raises(RateLimitError) as raised:
        await _client(network).get("series", expect=nothing)

    assert (raised.value.attempts, raised.value.retry_after, raised.value.retryable) == (1, _TOO_LONG, True)
    assert "Retry-After 100.0s exceeds max_retry_after 30.0s" in raised.value.__notes__


async def test_a_503_asking_too_long_raises_an_upstream_status_error_with_its_retry_after() -> None:
    """Raise ``UpstreamStatusError`` for a 503 whose ``Retry-After`` is past the ceiling."""
    network = FaultyTransport(schedule=[Respond(503, headers={"retry-after-ms": "45000"}), Respond(200)])

    with pytest.raises(UpstreamStatusError) as raised:
        await _client(network).get("series", expect=nothing)

    assert raised.value.retry_after == pytest.approx(45.0)
    assert len(network.calls) == 1


async def test_a_surviving_408_is_marked_retryable() -> None:
    """Mark a 408 that outlasted the attempts as retryable, though the status ladder raises it."""
    network = FaultyTransport(schedule=[Respond(408)] * _ATTEMPTS)

    with pytest.raises(UpstreamStatusError) as raised:
        await _client(network).get("series", expect=nothing)

    assert (raised.value.status_code, raised.value.retryable, raised.value.attempts) == (408, True, _ATTEMPTS)


async def test_a_post_is_resent_only_with_an_idempotency_key() -> None:
    """Send a plain POST once after a 503, and re-send one the policy stamps with an idempotency key."""
    plain = FaultyTransport(schedule=[Respond(503), Respond(201)])
    keyed = FaultyTransport(schedule=[Respond(503), Respond(201, json={"id": 7})])

    with pytest.raises(UpstreamStatusError):
        await _client(plain).post("orders", expect=nothing, json={"qty": 1})
    order = await _client(keyed, idempotency=IdempotencyKeys()).post(
        "orders", expect=json_reader(json.loads), json={"qty": 1}
    )

    assert len(plain.calls) == 1
    assert order == {"id": 7}
    keys = {call.headers["Idempotency-Key"] for call in keyed.calls}
    assert len(keyed.calls) == len(("first", "retry"))
    assert len(keys) == 1, "every attempt carries the same key"


@pytest.mark.parametrize("body", [None, {"qty": 1}], ids=["no-body", "json-body"])
async def test_a_post_dropped_before_it_was_written_is_resent_with_or_without_a_body(body: object) -> None:
    """Re-send a plain POST whose connection dropped before a byte was written, whether or not it carries a body."""
    network = FaultyTransport(schedule=[DropMidBody(after_bytes=0), Respond(201)])

    await _client(network).post("orders", expect=nothing, json=body)

    assert len(network.calls) == len(("dropped", "resent"))
    assert network.calls[0].bytes_sent == 0


@pytest.mark.parametrize("body", [None, {"qty": 1}], ids=["no-body", "json-body"])
async def test_a_post_dropped_inside_its_head_is_not_resent(body: object) -> None:
    """Send a plain POST once when its head had started to go out, as httpx2's trace counts it written."""
    network = FaultyTransport(schedule=[DropMidBody(after_bytes=3), Respond(201)])

    with pytest.raises(TransportError) as raised:
        await _client(network).post("orders", expect=nothing, json=body)

    assert raised.value.retryable is False
    assert len(network.calls) == 1


async def test_a_whole_retry_sequence_counts_once_toward_the_breaker() -> None:
    """Charge one failure to the host's breaker for a call that failed on every attempt."""
    network = FaultyTransport(schedule=[Respond(503)] * _ATTEMPTS)

    with pytest.raises(UpstreamStatusError):
        await _client(network).get("series", expect=nothing)

    assert len(network.calls) == _ATTEMPTS
    assert failures(_HOST) == 1


async def test_the_budget_is_shared_by_every_call_of_a_client() -> None:
    """Refuse the second call's retry once the first call spent the client's budget."""
    budget = RetryBudget(ratio=0.0, min_per_second=0.1, window=10.0)
    network = FaultyTransport(schedule=[Respond(503), Respond(200), Respond(503)])
    client = _client(network, RetryPolicy(backoff=_FULL, budget=budget))

    await client.get("series", expect=nothing)
    with pytest.raises(UpstreamStatusError) as raised:
        await client.get("series", expect=nothing)

    assert raised.value.retryable is True
    assert raised.value.__notes__[-1] == "retry budget exhausted"


async def test_an_exhausted_deadline_stops_the_retries() -> None:
    """Stop retrying when the backoff would leave no time before the call's deadline."""
    clock = TestClock()
    network = FaultyTransport(schedule=[Slow(seconds=60), Respond(200)], sleep=clock.sleep)
    with timing_override(Timing(clock=clock, sleep=clock.sleep)):
        client = _client(network, deadline=2.0)

    with pytest.raises(TransportError) as raised:
        await client.get("series", expect=nothing)

    assert len(network.calls) == 1
    assert clock.now == pytest.approx(2.0), "the attempt's read timeout was cut to the deadline"
    assert "deadline exhausted" in raised.value.__notes__


async def test_retries_are_announced_to_the_clients_observers() -> None:
    """Deliver ``RetryScheduled`` to the client's observers for each retry."""
    observer = _Events()
    network = FaultyTransport(schedule=[Respond(503), Respond(200)])
    client = StubClient(
        base_url=f"https://{_HOST}",
        rate=Rate.per_second(1000),
        policy=TransportPolicy(retry=RetryPolicy(backoff=_FULL, budget=None)),
        http=HttpOptions(transport=network, observers=(observer,)),
    )

    await client.get("series", expect=nothing)

    scheduled = [event for event in observer.events if isinstance(event, RetryScheduled)]
    assert [(event.attempt, event.reason) for event in scheduled] == [(2, "503")]


async def test_x_should_retry_false_stops_a_retryable_status() -> None:
    """Send once when the server answers ``x-should-retry: false``, and mark the error not retryable, as it said."""
    network = FaultyTransport(schedule=[Respond(503, headers={"x-should-retry": "false"}), Respond(200)])

    with pytest.raises(UpstreamStatusError) as raised:
        await _client(network).get("series", expect=nothing)

    assert len(network.calls) == 1
    assert raised.value.retryable is False
