# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``policy_disabled()``: build a client with its transport policy (breaker, retries, pacing) switched off.

The tests pin each of the three behaviours switched off, plus the properties that make it a construction-time switch
rather than a global one. They observe what a client does (how many sends a 503 gets, whether an open breaker refuses
it, the rate each budget is built at) rather than timing, so they cannot flake on a slow machine.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import HttpOptions, TransportPolicy
from headgate._client import AsyncBaseClient
from headgate._core import policy_is_disabled
from headgate._responses import nothing
from headgate.breaker._registry import DEFAULT_REGISTRY
from headgate.errors import TransportError, UpstreamStatusError
from headgate.policy import policy_disabled
from headgate.ratelimit import UNLIMITED, Rate, RateBudget, RateRule
from headgate.retry import RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Callable

    from headgate._core import CallContext

_HOST = "example.test"
_RATE = Rate.per_second(2.0)
_RULE_RATE = Rate.per_second(8.0)
_MAX_ATTEMPTS = 3

#: More consecutive failures than any breaker threshold, so the loop that opens one always terminates.
_ENOUGH_FAILURES = 50


class _Wire:
    """A transport handler that counts requests and answers each with one fixed status."""

    def __init__(self, status: int) -> None:
        """Answer every request with *status*."""
        self.status = status
        self.sends = 0

    def __call__(self, _request: httpx2.Request) -> httpx2.Response:
        """Count the request and answer it."""
        self.sends += 1
        return httpx2.Response(self.status)


class _Client(AsyncBaseClient):
    """A typical subclass: it fixes its own policy in ``super().__init__()`` and exposes none of it."""

    def __init__(self, wire: Callable[[httpx2.Request], httpx2.Response]) -> None:
        """Build over *wire*, with a secondary budget for ``/r``.

        The ``rate_limits`` rule is why the switch is enforced where budgets are built rather than by overriding the
        client's ``rate=``, which would leave the secondary budget throttled.
        """
        super().__init__(
            base_url=f"https://{_HOST}",
            rate=_RATE,
            policy=TransportPolicy(
                retry=RetryPolicy(attempts=_MAX_ATTEMPTS), rate_limits=(RateRule("/r", _RULE_RATE),)
            ),
            http=HttpOptions(transport=httpx2.MockTransport(wire)),
        )


async def _sends_for_a_503(client_factory: Callable[[_Wire], _Client]) -> int:
    """Return how many sends one call through a fresh client gets when every attempt answers 503."""
    wire = _Wire(httpx2.codes.SERVICE_UNAVAILABLE)
    with pytest.raises(UpstreamStatusError):
        await client_factory(wire).get("x", expect=nothing)
    return wire.sends


async def _open_the_breaker() -> None:
    """Fail a normally built client against the host until the shared breaker refuses it."""
    failing = _Client(_Wire(httpx2.codes.SERVICE_UNAVAILABLE))
    for _ in range(_ENOUGH_FAILURES):
        if any(breaker.refusing for breaker in DEFAULT_REGISTRY):
            return
        with pytest.raises(UpstreamStatusError):
            await failing.get("x", expect=nothing)
    msg = "the breaker never opened"
    raise AssertionError(msg)


def _built_off(wire: _Wire) -> _Client:
    with policy_disabled():
        return _Client(wire)


class TestPolicyOff:
    """With ``policy_disabled()`` around construction, a client keeps none of its transport policy."""

    async def test_an_open_breaker_does_not_refuse_it(self) -> None:
        """Check that a policy-off client is not refused by a breaker another client opened on the same host.

        Not recording failures is not enough: the registry is shared, so a client that still read it would be refused.
        """
        await _open_the_breaker()
        with pytest.raises(TransportError, match="circuit"):
            await _Client(_Wire(httpx2.codes.OK)).get("x", expect=nothing)
        wire = _Wire(httpx2.codes.OK)
        await _built_off(wire).get("x", expect=nothing)
        assert wire.sends == 1

    async def test_retries_are_zero_not_merely_instant(self) -> None:
        """Check that policy-off removes the retries themselves, not just the backoff between them."""
        assert await _sends_for_a_503(_Client) == _MAX_ATTEMPTS, "control: a normally built client retries"
        assert await _sends_for_a_503(_built_off) == 1

    async def test_primary_and_secondary_budgets_are_both_unthrottled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Check that both the default budget and a rule's budget are built unlimited, not at the client's rates."""
        built: list[tuple[float, float]] = []

        async def recording_wait(budget: RateBudget, _context: CallContext) -> None:
            built.append((budget.rate.requests, budget.rate.period))

        monkeypatch.setattr(RateBudget, "wait", recording_wait)

        async def rates(client: _Client) -> list[tuple[float, float]]:
            built.clear()
            await client.get("x", expect=nothing)
            await client.get("r", expect=nothing)
            return list(built)

        assert await rates(_Client(_Wire(httpx2.codes.OK))) == [
            (_RATE.requests, _RATE.period),
            (_RULE_RATE.requests, _RULE_RATE.period),
        ], "control"
        assert await rates(_built_off(_Wire(httpx2.codes.OK))) == [(UNLIMITED.requests, UNLIMITED.period)] * 2


class TestItIsReadAtConstruction:
    """The switch is a task-local setting read when a client is built."""

    def test_the_flag_does_not_leak_past_the_block(self) -> None:
        """Check that leaving the block switches policy back on."""
        assert not policy_is_disabled()
        with policy_disabled():
            assert policy_is_disabled()
        assert not policy_is_disabled()

    def test_nesting_restores_the_enclosing_value_not_the_default(self) -> None:
        """Check that leaving an inner block restores the outer block's setting."""
        with policy_disabled():
            with policy_disabled():
                assert policy_is_disabled()
            assert policy_is_disabled(), "the inner exit must not switch policy back on"

    async def test_an_already_built_client_is_unaffected(self) -> None:
        """Check that entering the block around an existing client changes nothing, by design."""
        wire = _Wire(httpx2.codes.SERVICE_UNAVAILABLE)
        client = _Client(wire)
        with policy_disabled(), pytest.raises(UpstreamStatusError):
            await client.get("x", expect=nothing)
        assert wire.sends == _MAX_ATTEMPTS

    async def test_the_setting_is_task_local(self) -> None:
        """Check that the setting is a ContextVar: a sibling task cannot see it."""
        seen: list[bool] = []

        async def sibling() -> None:
            await asyncio.sleep(0)
            seen.append(policy_is_disabled())

        async def holder() -> None:
            with policy_disabled():
                await asyncio.sleep(0)
                seen.append(policy_is_disabled())

        await asyncio.gather(holder(), sibling())
        assert seen.count(True) == 1, "exactly the holding task sees it"
        assert seen.count(False) == 1
