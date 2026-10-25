"""``policy_disabled()``: build a client with its transport policy (breaker, retries, pacing) switched off.

The tests pin each of the three behaviours switched off, plus the properties that make it a construction-time
switch rather than a global one. They assert on the assembled transport stack and on the limiters' effective rates
rather than on timing, so they cannot flake on a slow machine.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx2

from headgate._breaker import HOST_BREAKERS, NO_BREAKERS
from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate, RateRule
from headgate._retry import RetryTransport
from headgate.policy import policy_disabled, policy_is_disabled
from tests.support.limiters import default_limiter, rate_limit_transport

if TYPE_CHECKING:
    from headgate._rate_limit import AsyncRateLimiter


class _Client(AsyncBaseClient):
    """A typical subclass: it fixes its own policy in ``super().__init__()`` and exposes none of it."""

    def __init__(self) -> None:
        # The ``rate_limits`` rule gives this client a SECONDARY limiter, built from the rule's own ``Rate``. It is
        # why the switch is enforced inside ``AsyncRateLimiter`` rather than by overriding the client's ``rate=``,
        # which would leave the secondary limiter throttled.
        super().__init__(
            base_url="https://example.test",
            rate=Rate.per_second(2.0),
            max_attempts=3,
            rate_limits=[RateRule("/r", Rate.per_second(8.0))],
        )


def _rule_limiter(client: AsyncBaseClient) -> AsyncRateLimiter:
    """The limiter ``_Client``'s one ``rate_limits`` rule routes ``/r`` to (not the default's)."""
    limiters = rate_limit_transport(client)._limiters
    limiter = limiters.for_request(httpx2.Request("GET", "https://example.test/r"))
    assert limiter is not limiters.default, "the rule must route its own path to its own limiter"
    return limiter


def _stack_has_breaker(client: AsyncBaseClient) -> bool:
    """Whether this client consults the shared breaker registry (``HOST_BREAKERS``) rather than ``NO_BREAKERS``."""
    return client._breakers is HOST_BREAKERS


class TestPolicyOff:
    def test_breaker_is_not_consulted(self) -> None:
        """A policy-off client reads ``NO_BREAKERS``, not the process-global registry.

        Not recording failures is not enough: the registry is shared, so a client that still read it would be
        REFUSED by a breaker some other client opened on the same host. This pins the registry choice; the refusal
        itself is pinned end to end in ``tests/transport/test_breaker_integration.py``.
        """
        with policy_disabled():
            off = _Client()
        on = _Client()
        assert _stack_has_breaker(on), "control: a normally-built client consults its breaker"
        assert off._breakers is NO_BREAKERS

    def test_retries_are_zero_not_merely_instant(self) -> None:
        """Policy-off removes the retries themselves, not just the backoff between them.

        ``build_retry`` takes the ATTEMPT count and sets the retry policy's ``total`` (a RETRY count) to
        ``max(n - 1, 0)``, so policy-off is ``total == 0``: one attempt, never repeated.
        ``TestPolicyOnIsUnchanged`` pins the other end of that mapping.
        """
        with policy_disabled():
            off = _Client()
        transport = off._http_client._transport
        assert isinstance(transport, RetryTransport)
        assert transport.retry.total == 0

    def test_primary_and_secondary_limiters_are_both_unthrottled(self) -> None:
        with policy_disabled():
            off = _Client()
        on = _Client()
        assert default_limiter(on)._max_rate == 2.0, "control"
        assert _rule_limiter(on)._max_rate == 8.0, "control"
        assert default_limiter(off)._max_rate == 1_000_000.0
        assert _rule_limiter(off)._max_rate == 1_000_000.0, "a secondary limiter must be covered too"


class TestItIsReadAtConstruction:
    def test_the_flag_does_not_leak_past_the_block(self) -> None:
        assert not policy_is_disabled()
        with policy_disabled():
            assert policy_is_disabled()
        assert not policy_is_disabled()

    def test_nesting_restores_the_enclosing_value_not_the_default(self) -> None:
        with policy_disabled():
            with policy_disabled():
                assert policy_is_disabled()
            assert policy_is_disabled(), "the inner exit must not switch policy back on"

    def test_an_already_built_client_is_unaffected(self) -> None:
        """Entering the block around an existing client changes nothing, by design."""
        on = _Client()
        with policy_disabled():
            assert _stack_has_breaker(on)
            assert on._breakers is HOST_BREAKERS

    async def test_the_setting_is_task_local(self) -> None:
        """A ContextVar, not a module global: a sibling task cannot see the setting."""
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


class TestPolicyOnIsUnchanged:
    """Without ``policy_disabled()`` a client keeps every policy."""

    def test_default_construction_keeps_every_policy(self) -> None:
        client = _Client()
        assert _stack_has_breaker(client)
        # max_attempts=3 attempts ⇒ total=2 retries (see build_retry).
        transport = client._http_client._transport
        assert isinstance(transport, RetryTransport)
        assert transport.retry.total == 2
        assert default_limiter(client)._max_rate == 2.0
