"""Construct a client with its transport policy switched off.

``AsyncBaseClient`` wraps every request in three production behaviours: retry with
exponential backoff, a per-host circuit breaker, and an async rate limiter. All three
are correct in production and actively hostile anywhere the network is a fake — a
mocked leaf under a test, or a connectivity probe whose whole job is to report the
first failure rather than paper over it with three retries and a 5s backoff.

The knobs exist on :meth:`AsyncBaseClient.__init__` (``max_retries``, ``circuit_breaker``,
``rate``) but are unreachable in practice: every client class hardcodes its own
policy in ``super().__init__()`` and exposes none of it. So the switch is read at
construction instead, from a context variable this module owns::

    with policy_disabled():
        client = ExampleClient(ExampleSignedAuth(key_id=key_name, secret=pem))
    async with client:  # construction read the switch; the block need not stay open
        ...

    # or, for a whole test directory, in that directory's own conftest.py:
    @pytest.fixture(autouse=True)
    def _no_transport_policy():
        with policy_disabled():
            yield

**It is a CONSTRUCTION-time switch, and only that.** A client reads the variable while
it is being built and remembers the answer; entering the block around an already-built
client changes nothing. That is deliberate — the transport stack is assembled in
``__init__`` and is immutable afterwards, so a request-time switch would have to
rebuild it mid-flight.

A ``ContextVar`` rather than a module global because it is task-local: two concurrent
asyncio tasks constructing clients cannot see each other's setting, and the ``reset``
in the ``finally`` cannot leak into a sibling task the way a global would.

**What "off" means, precisely:**

* ``max_retries=0`` — no attempt is retried. STRONGER than a zeroed backoff, which keeps
  the retries and makes them instant. A suite testing retry BEHAVIOUR wants that instead
  and must not use this seam.
* ``circuit_breaker=False`` — the client looks its host up in ``NO_BREAKERS``, which records
  nothing AND never reads the process-global registry, so a policy-off client cannot be
  refused service by a breaker some other client opened on the same host.
* an effectively-unlimited rate — enforced in :class:`AsyncRateLimiter` itself rather than
  by the client replacing the ``Rate`` it was given, so it also covers the SECONDARY
  limiters a client declares (example's candle ``rate_limits`` rule, whose limiter
  :class:`~headgate._transports.PathLimiters` builds from the rule's own ``Rate``), which
  a swap of the client's own ``rate=`` would leave at their declared budget.

Nothing here is test-only: the connectivity probes construct their clients through this
same seam, which is why it lives in the package proper rather than in test scaffolding.
(Test scaffolding proper stays out of the shipped package.)
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["policy_disabled", "policy_is_disabled"]

#: Task-local: is transport policy switched off for clients built right now?
_POLICY_OFF: ContextVar[bool] = ContextVar("headgate_policy_off", default=False)


@contextmanager
def policy_disabled() -> Generator[None]:
    """Build clients with retry, the circuit breaker, and rate limiting switched off.

    Affects only clients CONSTRUCTED inside the block; see the module docstring.
    Re-entrant, and safe to nest — the token reset restores the enclosing value rather
    than assuming the block was the outermost one.
    """
    token = _POLICY_OFF.set(True)
    try:
        yield
    finally:
        _POLICY_OFF.reset(token)


def policy_is_disabled() -> bool:
    """Whether a client being constructed right now should skip its transport policy."""
    return _POLICY_OFF.get()
