"""The construction seam that builds a client with its transport policy switched off.

Each test below pins one of the three behaviours the seam claims, plus the two
properties that make it a CONSTRUCTION seam rather than a global switch. The tests
assert on the assembled transport stack and on the limiter's effective rate rather
than on timing, so they cannot flake on a slow box.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx2

from headgate._async_breaker import NO_BREAKERS, _host_breakers
from headgate._base import AsyncBaseClient
from headgate._rate_limiter import Rate
from headgate._transports import RetryTransport
from headgate.policy import policy_disabled, policy_is_disabled
from tests.support.limiters import default_limiter, rate_limit_transport

if TYPE_CHECKING:
    from headgate._rate_limiter import AsyncRateLimiter


class _Client(AsyncBaseClient):
    """Minimal stand-in for the 84 real subclasses: hardcodes policy, exposes none."""

    def __init__(self) -> None:
        # A SECONDARY limiter the subclass declares — as example declares the one its candle ``rate_limits`` rule
        # carries, which ``PathLimiters`` builds from the rule's own ``Rate`` — and the reason the seam is enforced
        # inside AsyncRateLimiter rather than at the client: replacing the client's own ``rate=`` would leave it.
        super().__init__(
            base_url="https://example.test",
            rate=Rate.per_second(2.0),
            max_retries=3,
            rate_limits=[("r", "/r", Rate.per_second(8.0))],
        )


def _rule_limiter(client: AsyncBaseClient) -> AsyncRateLimiter:
    """The limiter ``_Client``'s one ``rate_limits`` rule routes ``/r`` to (not the default's)."""
    limiters = rate_limit_transport(client)._limiters
    limiter = limiters.for_request(httpx2.Request("GET", "https://example.test/r"))
    assert limiter is not limiters.default, "the rule must route its own path to its own limiter"
    return limiter


def _stack_has_breaker(client: AsyncBaseClient) -> bool:
    """Whether this client consults the breaker at all.

    It was ``isinstance(transport, BreakerTransport)`` while the breaker was a layer, then an instance flag;
    it is the registry the client reads now — the shared one, or ``NO_BREAKERS``, which never reads it.
    """
    return client._breakers is _host_breakers


class TestPolicyOff:
    def test_breaker_is_not_consulted(self) -> None:
        with policy_disabled():
            off = _Client()
        on = _Client()
        assert _stack_has_breaker(on), "control: a normally-built client consults its breaker"
        assert not _stack_has_breaker(off)

    def test_breaker_consultation_is_skipped_too(self) -> None:
        """The second half of "no breaker": _send reads a process-global registry.

        Not recording failures is only half of "no breaker": the registry is process-global,
        so without ``NO_BREAKERS`` a policy-off client would still be REFUSED by a breaker
        some other client opened on the same host. This pins the registry choice; the refusal itself is
        pinned end to end in ``test_circuit_breaker.py``.
        """
        with policy_disabled():
            off = _Client()
        assert off._breakers is NO_BREAKERS
        assert _Client()._breakers is _host_breakers

    def test_retries_are_zero_not_merely_instant(self) -> None:
        """Stronger than the fixtures it replaces, which kept the retries and zeroed backoff.

        ``build_retry`` takes the ATTEMPT count and sets the retry policy's ``total``
        (a RETRY count) to ``max(n - 1, 0)``, so policy-off is ``total == 0``: one
        attempt, never repeated. The control below pins the other end of that mapping
        so a change to the convention fails here rather than silently weakening this.
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


class TestItIsAConstructionSeam:
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
            assert on._breakers is _host_breakers

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
    """The seam must be inert when nobody asks for it — the production path."""

    def test_default_construction_keeps_every_policy(self) -> None:
        client = _Client()
        assert _stack_has_breaker(client)
        # max_retries=3 attempts ⇒ total=2 retries (see build_retry).
        transport = client._http_client._transport
        assert isinstance(transport, RetryTransport)
        assert transport.retry.total == 2
        assert default_limiter(client)._max_rate == 2.0
        assert client._breakers is _host_breakers
