# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The construction-time switch that builds clients with their call policy off.

It lives here, below every layer, because a rate budget honors it when it is built, whoever builds it, and the
policy subpackages may import only this core. :mod:`headgate.testing` exports it.

A ``ContextVar`` rather than a module global, so two concurrent tasks constructing clients cannot see each other's
setting.
"""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["policy_disabled", "policy_is_disabled"]

#: Task-local: is call policy switched off for clients built right now?
_POLICY_OFF: ContextVar[bool] = ContextVar("headgate_policy_off", default=False)


@contextmanager
def policy_disabled() -> Generator[None]:
    """Build the clients constructed in the block with retry, the circuit breaker and rate limiting switched off.

    For a test: the call policy is right in production and in the way where the network is a fake and the test asserts
    on the first failure as the fake sends it. An SDK usually fixes its policy where it builds its ``Client``, so the
    switch is read when a client is built, not passed to it::

        with policy_disabled():
            client = ExampleClient(api_key="k")
        async with client:  # construction read the switch; the block need not stay open
            ...

    It is a construction-time switch, and only that: entering the block around a client already built changes nothing,
    since its stack is fixed. Safe to nest. What "off" means:

    * no request is retried, whatever the policy, a route, a view or a call asks for. Stronger than
      :func:`~headgate.testing.instant_waits`, which keeps the retries and makes every wait instant, for a test of
      retry behavior;
    * no breaker (``CallPolicy(breaker=None)``): the client never reads a registry, so a breaker another client opened
      on the same host cannot refuse it;
    * a budget that paces nothing, enforced where a budget is built, so it covers the budgets a client's routes build
      from ``rate=`` too, and a client ignores a shared ``RateBudget`` passed in.
    """
    token = _POLICY_OFF.set(True)
    try:
        yield
    finally:
        _POLICY_OFF.reset(token)


def policy_is_disabled() -> bool:
    """Return whether a client being constructed right now should skip its call policy."""
    return _POLICY_OFF.get()
