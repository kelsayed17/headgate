# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The retry budget's sliding window, and the classification of each attempt's result."""

from __future__ import annotations

import h2.errors
import h2.events
import httpcore2
import httpx2
import pytest

from headgate._core import SendProgress
from headgate.retry import RetryBudget, RetryPolicy
from headgate.retry._budget import RetryLedger
from headgate.retry._classify import Attempted, classify, refused_stream
from tests.support.clock import FakeClock

#: A window, in seconds, for the budget tests.
_WINDOW = 10.0

#: How many retries a budget with a floor of one per second allows over the window, with no first attempts.
_FLOOR_RETRIES = 10


# --- RetryLedger ---


def test_the_floor_admits_min_per_second_times_window_retries() -> None:
    """Admit exactly ``min_per_second · window`` retries with no first attempts, then refuse."""
    clock = FakeClock()
    ledger = RetryLedger(RetryBudget(ratio=0.0, min_per_second=1.0, window=_WINDOW), clock)

    admitted = [ledger.try_spend() for _ in range(_FLOOR_RETRIES + 1)]

    assert admitted == [True] * _FLOOR_RETRIES + [False]


def test_first_attempts_earn_retries_their_ratio() -> None:
    """Admit ``ratio`` of a retry per first attempt, beyond a zero floor."""
    ledger = RetryLedger(RetryBudget(ratio=0.5, min_per_second=0.0, window=_WINDOW), FakeClock())
    for _ in range(4):
        ledger.record_first()

    assert [ledger.try_spend() for _ in range(3)] == [True, True, False]


def test_the_window_slides_so_old_retries_stop_counting() -> None:
    """Forget the retries made more than a window ago, so the budget refills as time passes."""
    clock = FakeClock()
    ledger = RetryLedger(RetryBudget(ratio=0.0, min_per_second=0.1, window=_WINDOW), clock)

    assert ledger.try_spend() is True
    assert ledger.try_spend() is False
    clock.advance(_WINDOW / 2)
    assert ledger.try_spend() is False, "half a window later the retry still counts"
    clock.advance(_WINDOW)
    assert ledger.try_spend() is True


def test_a_budget_measures_its_window_on_its_own_clock_when_it_has_one() -> None:
    """Prefer the budget's own clock to the one the transport supplies."""
    own, transport = FakeClock(), FakeClock()
    ledger = RetryLedger(RetryBudget(ratio=0.0, min_per_second=0.1, window=_WINDOW, clock=own), transport)
    assert ledger.try_spend() is True

    transport.advance(_WINDOW * 2)
    assert ledger.try_spend() is False, "the transport's clock moving does not slide the window"
    own.advance(_WINDOW * 2)
    assert ledger.try_spend() is True


# --- classify ---


def _attempted(method: str = "POST", *, keyed: bool = False, connected: bool = False, sent: int = 0) -> Attempted:
    """Return an attempt of *method* with the given progress."""
    return Attempted(method=method, keyed=keyed, progress=SendProgress(connected=connected, bytes_sent=sent))


def _refused() -> httpx2.RemoteProtocolError:
    """Return the error httpx2 raises for a stream the server reset with ``REFUSED_STREAM``."""
    event = h2.events.StreamReset(stream_id=3, error_code=h2.errors.ErrorCodes.REFUSED_STREAM)
    cause = httpcore2.RemoteProtocolError(event)
    error = httpx2.RemoteProtocolError(str(cause))
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("result", "attempted", "retryable"),
    [
        pytest.param(httpx2.Response(503), _attempted("GET"), True, id="503-get"),
        pytest.param(httpx2.Response(503), _attempted(), False, id="503-post"),
        pytest.param(httpx2.Response(503), _attempted(keyed=True), True, id="503-post-keyed"),
        pytest.param(httpx2.Response(425), _attempted(), True, id="425-post-not-processed"),
        pytest.param(httpx2.ReadTimeout("t"), _attempted("PUT", connected=True, sent=9), True, id="timeout-put"),
        pytest.param(httpx2.ReadTimeout("t"), _attempted(connected=True, sent=9), False, id="timeout-post-sent"),
        pytest.param(httpx2.ReadError("r"), _attempted(connected=True), True, id="post-connected-nothing-written"),
        pytest.param(httpx2.ReadError("r"), _attempted(), False, id="post-unknown-progress"),
        pytest.param(httpx2.ConnectError("c"), _attempted(), True, id="post-never-connected"),
        pytest.param(httpx2.PoolTimeout("p"), _attempted(), True, id="post-pool-timeout"),
        pytest.param(_refused(), _attempted(connected=True, sent=40), True, id="post-refused-stream"),
        pytest.param(httpx2.LocalProtocolError("l"), _attempted("GET"), False, id="local-protocol"),
        pytest.param(httpx2.UnsupportedProtocol("u"), _attempted("GET"), False, id="unsupported-protocol"),
    ],
)
def test_a_failure_is_retryable_exactly_when_resending_is_safe(
    result: httpx2.Response | Exception, attempted: Attempted, *, retryable: bool
) -> None:
    """Retry an idempotent or keyed request, or one proven unprocessed or unsent; never a local mistake."""
    verdict = classify(result, attempted, RetryPolicy())

    assert verdict.failure is True
    assert verdict.retryable is retryable


def test_a_status_outside_the_policy_is_an_answer() -> None:
    """Return a 404 (or a 200) as an answer, not a failure."""
    for status in (200, 404):
        assert classify(httpx2.Response(status), _attempted("GET"), RetryPolicy()).failure is False


@pytest.mark.parametrize(
    ("status", "header", "retryable"),
    [(503, "false", False), (409, "true", True), (503, " TRUE ", True)],
)
def test_x_should_retry_overrides_the_status_rules(status: int, header: str, *, retryable: bool) -> None:
    """Follow the server's ``x-should-retry`` over the status set and the method rule."""
    response = httpx2.Response(status, headers={"x-should-retry": header})
    verdict = classify(response, _attempted(), RetryPolicy())

    assert (verdict.failure, verdict.retryable) == (True, retryable)


def test_x_should_retry_is_ignored_when_the_policy_says_so() -> None:
    """Classify by the status rules alone with ``respect_should_retry=False``."""
    policy = RetryPolicy(respect_should_retry=False)
    response = httpx2.Response(409, headers={"x-should-retry": "true"})

    assert classify(response, _attempted(), policy).failure is False


def test_a_verdict_names_the_reason_and_the_delay_asked() -> None:
    """Name the status or exception type, and carry a 429's ``Retry-After``."""
    verdict = classify(httpx2.Response(429, headers={"Retry-After": "3"}), _attempted("GET"), RetryPolicy())

    assert (verdict.reason, verdict.retry_after) == ("429", 3.0)
    assert classify(_refused(), _attempted(), RetryPolicy()).reason == "REFUSED_STREAM"


def test_only_a_refused_stream_counts_as_not_processed() -> None:
    """Treat another stream reset, a non-h2 argument, or a short cause chain as an ordinary failure."""
    other = h2.events.StreamReset(stream_id=1, error_code=h2.errors.ErrorCodes.CANCEL)
    cancelled = httpx2.RemoteProtocolError("reset")
    cancelled.__cause__ = httpcore2.RemoteProtocolError(other)

    assert refused_stream(cancelled) is False
    assert refused_stream(httpx2.RemoteProtocolError("Server disconnected")) is False
    assert refused_stream(_refused()) is True
