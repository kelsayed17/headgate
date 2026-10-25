"""Construct a client with its transport policy switched off.

``AsyncBaseClient`` wraps every request in retry with backoff, a per-host circuit breaker, and a rate limiter. All
three are right in production and wrong where the network is a fake (a mocked transport under a test) or where the job
is to report the FIRST failure (a connectivity probe).

The knobs exist on ``AsyncBaseClient.__init__`` (``max_attempts``, ``circuit_breaker``, ``rate``), but a client class
usually fixes its own policy in ``super().__init__()`` and exposes none of it. So the switch is read at construction
from a context variable instead::

    with policy_disabled():
        client = ExampleClient(api_key="k")
    async with client:  # construction read the switch; the block need not stay open
        ...

    # or, for a whole test directory, in that directory's own conftest.py:
    @pytest.fixture(autouse=True)
    def _no_transport_policy():
        with policy_disabled():
            yield

**It is a construction-time switch, and only that.** A client reads the variable while it is being built; entering
the block around an already-built client changes nothing, because the transport stack is immutable once built.

A ``ContextVar`` rather than a module global, so two concurrent tasks constructing clients cannot see each other's
setting.

**What "off" means:**

* ``max_attempts=0``: no attempt is retried. Stronger than a zeroed backoff, which keeps the retries and makes them
  instant; a suite testing retry behaviour wants that instead.
* ``circuit_breaker=False``: the client never reads the process-global breaker registry, so it cannot be refused by
  a breaker another client opened on the same host.
* an effectively unlimited rate, enforced in the rate limiter itself, so it also covers the limiters a client's
  ``rate_limits`` rules build.
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

    Affects only clients CONSTRUCTED inside the block; see the module docstring. Safe to nest.
    """
    token = _POLICY_OFF.set(True)
    try:
        yield
    finally:
        _POLICY_OFF.reset(token)


def policy_is_disabled() -> bool:
    """Whether a client being constructed right now should skip its transport policy."""
    return _POLICY_OFF.get()
