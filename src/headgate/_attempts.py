# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The attempt layer: reports every send of a call to the client's observers as an ``AttemptFinished`` event.

It sits directly under the retry layer (see :mod:`headgate._stack`), which begins each attempt on the call's context,
and is installed only on a client with observers.
"""

from __future__ import annotations

from typing import override

import httpx2

from headgate._core import AttemptFinished, CallContext

__all__ = ["AttemptTransport"]


class AttemptTransport(httpx2.AsyncBaseTransport):
    """Emit one ``AttemptFinished`` per send: the response head's status, or the type of what the send raised."""

    def __init__(self, *, transport: httpx2.AsyncBaseTransport) -> None:
        """Report the sends made through *transport*."""
        self._transport = transport

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        context = CallContext.of(request)
        started = context.clock()
        try:
            response = await self._transport.handle_async_request(request)
        except BaseException as exc:
            context.emit(
                AttemptFinished(
                    call=context.info,
                    attempt=context.attempt,
                    error=type(exc).__name__,
                    elapsed=context.clock() - started,
                )
            )
            raise
        context.emit(
            AttemptFinished(
                call=context.info,
                attempt=context.attempt,
                status_code=response.status_code,
                elapsed=context.clock() - started,
            )
        )
        return response

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self._transport.aclose()
