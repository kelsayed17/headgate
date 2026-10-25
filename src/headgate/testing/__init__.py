# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A kit for testing a client built on headgate, without patching anything private.

* :class:`FakeTransport`: the network, as a route table of canned replies with a log of what was sent; pass it as
  ``HttpOptions(transport=...)``.
* :class:`FaultyTransport` (:mod:`headgate.testing.faults`): a network that fails on a schedule (a connect failure, a
  status, a dropped connection, a slow answer, a refused HTTP/2 stream) and records what each attempt sent.
* :class:`TestClock`: a manual clock whose sleep advances it; :meth:`TestClock.install` builds clients on it.
* :func:`instant_retries`: clients whose retries do not wait.
* :func:`isolated_breakers`: clients whose circuit breakers live in a fresh registry.
* :class:`CassetteFilter`: vcrpy / pytest-recording filters that keep credentials and token mints out of cassettes.

The three context managers act on the clients **constructed** inside their block (the way a client class fixes its
options in ``super().__init__()`` leaves no other way in), so build the client inside them, typically in a fixture::

    @pytest.fixture
    def client() -> Iterator[MyClient]:
        with instant_retries(), isolated_breakers():
            yield MyClient(api_key="test")
"""

from headgate.testing._breakers import isolated_breakers
from headgate.testing._recording import CassetteFilter, RecordedRequest
from headgate.testing._time import TestClock, instant_retries
from headgate.testing._transport import FakeTransport, Reply, UnmatchedRequestError
from headgate.testing.faults import (
    ConnectFail,
    DropMidBody,
    FaultCall,
    FaultyTransport,
    PoolFull,
    RefusedStream,
    Respond,
    Slow,
    Step,
)

__all__ = [
    "CassetteFilter",
    "ConnectFail",
    "DropMidBody",
    "FakeTransport",
    "FaultCall",
    "FaultyTransport",
    "PoolFull",
    "RecordedRequest",
    "RefusedStream",
    "Reply",
    "Respond",
    "Slow",
    "Step",
    "TestClock",
    "UnmatchedRequestError",
    "instant_retries",
    "isolated_breakers",
]
