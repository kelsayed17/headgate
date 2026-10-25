# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A scripted web of origins for the security tests: each URL answers a redirect or a 200, every request recorded."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import httpx2

from headgate import HttpOptions, TransportPolicy
from headgate.ratelimit import Rate
from headgate.retry import NO_RETRY
from tests.support.clients import StubClient

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The client's own origin in these tests.
BASE = "https://api.example"


class Web:
    """A ``MockTransport`` handler: a URL in *redirects* answers 302 to its target, any other URL 200 ``ok``."""

    def __init__(self, redirects: Mapping[str, str] | None = None) -> None:
        """Redirect each URL (scheme, host, port and path; no query) in *redirects* to its target."""
        self._redirects = dict(redirects or {})
        self.sent: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record *request* and answer it."""
        self.sent.append(request)
        target = self._redirects.get(str(request.url.copy_with(query=None)))
        if target is not None:
            return httpx2.Response(httpx2.codes.FOUND, headers={"Location": target})
        return httpx2.Response(httpx2.codes.OK, text="ok")

    def client(
        self, *, auth: httpx2.Auth | None = None, http: HttpOptions | None = None, base_url: str = BASE
    ) -> StubClient:
        """Return a client on *base_url* over this web, sending each call once."""
        return StubClient(
            base_url=base_url,
            rate=Rate.per_second(1000),
            auth=auth,
            policy=TransportPolicy(retry=NO_RETRY),
            http=replace(http or HttpOptions(), transport=httpx2.MockTransport(self)),
        )
