"""A bare client for tests: the base client's policy and nothing of its own."""

from __future__ import annotations

from headgate import AsyncBaseClient


class StubClient(AsyncBaseClient):
    """``AsyncBaseClient`` under a name of its own, which every ``ClientError`` it raises carries as its source."""
