# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Test helpers for code built on headgate.

* :mod:`headgate.testing.faults`: :class:`FaultyTransport`, a network transport that fails on a schedule (a connect
  failure, a status, a dropped connection, a slow answer, a refused HTTP/2 stream) and records what each attempt sent.
"""

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
    "ConnectFail",
    "DropMidBody",
    "FaultCall",
    "FaultyTransport",
    "PoolFull",
    "RefusedStream",
    "Respond",
    "Slow",
    "Step",
]
