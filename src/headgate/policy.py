# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Construct a client with its transport policy switched off.

``AsyncBaseClient`` wraps every request in retry with backoff, a per-origin circuit breaker, and a rate budget. All
three are right in production and wrong where the network is a fake (a mocked transport under a test) or where the job
is to report the first failure (a connectivity probe).

The knobs exist on ``AsyncBaseClient.__init__`` (``TransportPolicy(retry=..., breaker=...)``, ``rate``), but a
client class usually fixes its own policy in ``super().__init__()`` and exposes none of it. So the switch is read at
construction from a context variable instead::

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
the block around an already-built client changes nothing, because the transport stack is fixed once built.

A ``ContextVar`` rather than a module global, so two concurrent tasks constructing clients cannot see each other's
setting.

What "off" means:

* ``TransportPolicy(retry=NO_RETRY)``: no request is retried, whatever a call or a route asks for. Stronger than an
  instant sleep, which keeps the retries and makes them instant; a suite testing retry behaviour wants that instead.
* ``TransportPolicy(breaker=None)``: the client never reads a breaker registry, so it cannot be refused by a breaker
  another client opened on the same host.
* an effectively unlimited rate, enforced where a budget is built, so it also covers the budgets a client's
  ``rate_limits`` rules build, and a shared ``RateBudget`` passed in.
"""

from headgate._core import policy_disabled

__all__ = ["policy_disabled"]
