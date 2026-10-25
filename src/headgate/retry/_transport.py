# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The retry layer: the top of every client's transport stack, re-sending a request its policy says to re-send."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx2

from headgate._core import CallContext, Sleep, asyncio_sleep
from headgate.retry._policy import NETWORK_ERRORS, RETRIED_METHODS, RETRIED_STATUSES, RETRY_POLICY

if TYPE_CHECKING:
    from headgate.retry._policy import RetryPolicy

__all__ = ["RetryTransport"]


class RetryTransport(httpx2.AsyncBaseTransport):
    """Re-send a request on a retried status or exception, waiting between attempts, and close what it wraps.

    * The policy is the call's own (``RETRY_POLICY`` on its :class:`~headgate._core.CallContext`, which a
      ``retry=False`` call sets to ``NO_RETRY``), otherwise :attr:`policy`.
    * Every send begins an attempt on the call's context, so the layers below and the error the call raises can count
      them.
    * A method outside ``RETRIED_METHODS`` is sent once.
    * A retried exception or status is re-sent until the policy's attempts are spent. A failed response is closed
      before the wait, which is computed by :meth:`RetryPolicy.wait_after <headgate.retry.RetryPolicy.wait_after>` and
      slept through *sleep*.
    * Running out re-raises the last exception or returns the last response.
    """

    def __init__(
        self, *, transport: httpx2.AsyncBaseTransport, policy: RetryPolicy, sleep: Sleep = asyncio_sleep
    ) -> None:
        """Re-send through *transport* under *policy*, waiting with *sleep*."""
        self._transport = transport
        self.policy = policy
        self._sleep = sleep

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        context = CallContext.of(request)
        policy = context.get(RETRY_POLICY) or self.policy
        if request.method.upper() not in RETRIED_METHODS:
            context.begin_attempt()
            return await self._transport.handle_async_request(request)
        retries_left = policy.attempts - 1
        retries = 0
        failure: httpx2.Response | Exception | None = None
        while True:
            if failure is not None:
                if isinstance(failure, httpx2.Response):
                    await failure.aclose()
                retries += 1
                await self._sleep(policy.wait_after(failure, retries))
            context.begin_attempt()
            try:
                response = await self._transport.handle_async_request(request)
            except Exception as exc:
                if retries >= retries_left or not isinstance(exc, NETWORK_ERRORS):
                    raise
                failure = exc
                continue
            if retries >= retries_left or response.status_code not in RETRIED_STATUSES:
                return response
            failure = response

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport (``httpx2.AsyncBaseTransport.aclose`` is a no-op)."""
        await self._transport.aclose()
