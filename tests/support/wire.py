# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A scripted network for client tests: answers in order, records every request it was sent."""

from __future__ import annotations

import httpx2


class Script:
    """A ``MockTransport`` handler serving *responses* in order (the last repeats) and recording each request."""

    def __init__(self, *responses: httpx2.Response | int) -> None:
        """Serve *responses*, a status standing for an empty response with it; a lone 200 when none are given."""
        self._responses = [httpx2.Response(r) if isinstance(r, int) else r for r in responses] or [httpx2.Response(200)]
        self.sent: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record *request* and answer it with the next scripted response."""
        self.sent.append(request)
        scripted = self._responses[min(len(self.sent), len(self._responses)) - 1]
        return httpx2.Response(
            scripted.status_code, headers=scripted.headers, content=scripted.content, request=request
        )

    def transport(self) -> httpx2.MockTransport:
        """Return a transport that answers through this script."""
        return httpx2.MockTransport(self)
