# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``policy_disabled()``: build a client with its transport policy (breaker, retries, pacing) switched off.

The tests pin each of the three behaviors switched off, plus the properties that make it a construction-time switch
rather than a global one. They observe what a client does (how many sends a 503 gets, whether an open breaker refuses
it, the rate each budget is built at) rather than timing, so they cannot flake on a slow machine.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import HttpOptions, Rate, RetryPolicy, Route, TransportPolicy, UpstreamStatusError
from headgate._client import Client
from headgate._core import policy_is_disabled
from headgate._responses import nothing
from headgate.policy import policy_disabled
from headgate.ratelimit._budget import Pacer
from headgate.ratelimit._rate import UNPACED
from headgate.testing import breakers_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from headgate._core import CallContext

_HOST = "example.test"
_RATE = Rate.per_second(2.0)
_RULE_RATE = Rate.per_second(8.0)
_MAX_ATTEMPTS = 3


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


def _client(wire: Callable[[httpx2.Request], httpx2.Response]) -> Client:
    """Build over *wire*, with a secondary budget for ``/r``.

    The route's ``rate=`` is why the switch is enforced where budgets are built rather than by overriding the client's
    ``rate=``, which would leave the secondary budget throttled.
    """
    return Client(
        base_url=f"https://{_HOST}",
        rate=_RATE,
        policy=TransportPolicy(retry=RetryPolicy(attempts=_MAX_ATTEMPTS), routes=(Route("/r", rate=_RULE_RATE),)),
        http=HttpOptions(transport=httpx2.MockTransport(wire)),
    )


async def _sends_for_a_503(client_factory: Callable[[_Wire], Client]) -> int:
    """Return how many sends one call through a fresh client gets when every attempt answers 503."""
    wire = _Wire(httpx2.codes.SERVICE_UNAVAILABLE)
    with pytest.raises(UpstreamStatusError):
        await client_factory(wire).get("x", expect=nothing)
    return wire.sends


def _built_off(wire: _Wire) -> Client:
    with policy_disabled():
        return _client(wire)


class TestPolicyOff:
    """With ``policy_disabled()`` around construction, a client keeps none of its transport policy."""

    def test_it_keeps_no_breaker(self) -> None:
        """Check that a policy-off client has no breaker to record in or be refused by, not merely one never tripped."""
        assert breakers_of(_client(_Wire(httpx2.codes.OK))) is not None, "control: a normally built client has one"
        assert breakers_of(_built_off(_Wire(httpx2.codes.OK))) is None

    async def test_retries_are_zero_not_merely_instant(self) -> None:
        """Check that policy-off removes the retries themselves, not just the backoff between them."""
        assert await _sends_for_a_503(_client) == _MAX_ATTEMPTS, "control: a normally built client retries"
        assert await _sends_for_a_503(_built_off) == 1

    async def test_primary_and_secondary_budgets_are_both_unthrottled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Check that both the default budget and a rule's budget are built unlimited, not at the client's rates."""
        built: list[tuple[float, float]] = []

        async def recording_wait(budget: Pacer, _context: CallContext) -> None:
            built.append((budget.rate.requests, budget.rate.period))

        monkeypatch.setattr(Pacer, "wait", recording_wait)

        async def rates(client: Client) -> list[tuple[float, float]]:
            built.clear()
            await client.get("x", expect=nothing)
            await client.get("r", expect=nothing)
            return list(built)

        assert await rates(_client(_Wire(httpx2.codes.OK))) == [
            (_RATE.requests, _RATE.period),
            (_RULE_RATE.requests, _RULE_RATE.period),
        ], "control"
        assert await rates(_built_off(_Wire(httpx2.codes.OK))) == [(UNPACED.requests, UNPACED.period)] * 2


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
        client = _client(wire)
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
