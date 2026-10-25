# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The retry layer: the top of every client's transport stack, re-sending a request when it is safe and worthwhile.

For each request :class:`RetryTransport` runs a sequence of attempts under the call's policy (``RETRY_POLICY`` on its
:class:`~headgate._core.CallContext`, else the transport's own):

1. **Send.** Each attempt begins an attempt on the call's context, with its read, write, connect and pool timeouts cut
   to the time left before the call's deadline. With a :class:`~headgate.retry.Hedge`, an idempotent request that has
   not answered after ``hedge.after`` seconds is sent again beside the first, and the first answer wins.
2. **Classify** the result (:mod:`headgate.retry._classify`): an answer is returned; a failure is retryable or not.
   The verdict is put on the context (``RETRY_OUTCOME``) for the error the client raises.
3. **Stop** with the last failure (re-raise the exception, or return the response for the client to raise) when it is
   not retryable, the attempts are spent, the body cannot be sent again (``REPLAYABLE_BODY``), the ``Retry-After``
   is past ``max_retry_after``, the wait would leave less than ``min_attempt_time`` before the deadline, the origin's
   breaker is refusing (``BREAKER_GATE``), or the retry budget is spent. Each stop that is not plain exhaustion adds a
   note: ``"the request body is a one-shot stream, so it is not sent again"``, ``"Retry-After 60.0s exceeds
   max_retry_after 30.0s"``, ``"deadline exhausted"``, ``"circuit opened"`` or ``"retry budget exhausted"``.
4. **Wait**: a ``Retry-After`` exactly, else the backoff; a failed response is first read (a bounded head of its body)
   and closed, so no connection is held through the wait. ``RetryScheduled`` is emitted for every retry and hedge.
   The deadline and the breaker are checked again after the wait, before the next attempt starts.

A hedge is sent as a copy of the request (its own headers and extensions, the same replayable body) on the same call
context, so concurrent sends share the context's send progress. That is sound because only idempotent methods are
hedged, and their retry decisions never depend on send progress.

Every attempt adds a history line to the call's notes, which the client attaches to the error it raises, for example
``attempt 2/3: 503 Service Unavailable (Retry-After 2.0s) after 0.41s`` or ``attempt 1/3: ReadTimeout after 30.0s``.
Lines name statuses and exception types only, never a URL, header or message, so they carry no credential.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, override

import httpx2

from headgate._core import BREAKER_GATE, CallContext, RetryScheduled, Sleep, asyncio_sleep
from headgate.retry._budget import RetryLedger
from headgate.retry._classify import Attempted, Verdict, classify
from headgate.retry._policy import NETWORK_ERRORS, REPLAYABLE_BODY, RETRY_OUTCOME, RETRY_POLICY, RetryOutcome

if TYPE_CHECKING:
    from headgate._core import Clock, SendProgress
    from headgate.retry._policy import Hedge, RetryBudget, RetryPolicy

__all__ = ["RetryTransport"]

#: The most of a failed response's body kept through the wait, for the error raised if no later attempt answers.
_PARKED_BODY_BYTES: Final = 64 * 1024

#: Headers describing the on-the-wire encoding, dropped from a parked response because its body is already decoded.
_WIRE_ENCODING_HEADERS: Final = frozenset({"content-encoding", "content-length", "transfer-encoding"})

#: The header an idempotency key is read from on a request no client call built.
_IDEMPOTENCY_HEADER: Final = "Idempotency-Key"

#: The reason a ``RetryScheduled`` event gives for a hedge.
_HEDGE_REASON: Final = "hedge"


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One send's result: the response or the exception, what classification made of it, and the request sent."""

    result: httpx2.Response | Exception
    verdict: Verdict
    request: httpx2.Request

    @property
    def retryable_failure(self) -> bool:
        """Whether this attempt failed in a way another attempt may fix."""
        return self.verdict.failure and self.verdict.retryable


def _rounded(seconds: float) -> str:
    """Return *seconds* as a history line writes it: rounded to hundredths, with an ``s``."""
    return f"{round(seconds, 2)}s"


def _describe(result: httpx2.Response | Exception, verdict: Verdict) -> str:
    """Return what a history line says of an attempt's result: the status and phrase, or the exception type."""
    if isinstance(result, httpx2.Response):
        phrase = httpx2.codes.get_reason_phrase(result.status_code)
        text = f"{result.status_code} {phrase}".rstrip()
        if verdict.retry_after is not None:
            text += f" (Retry-After {_rounded(verdict.retry_after)})"
        return text
    name = type(result).__name__
    return name if verdict.reason == name else f"{name} ({verdict.reason})"


async def _close(response: httpx2.Response) -> None:
    """Close *response*, suppressing the error closing a partly read compressed stream may raise."""
    with contextlib.suppress(httpx2.HTTPError):
        await response.aclose()


async def _parked(response: httpx2.Response, request: httpx2.Request) -> httpx2.Response:
    """Read a bounded head of *response*'s body, close it, and return an in-memory copy holding no connection."""
    chunks: list[bytes] = []
    total = 0
    with contextlib.suppress(httpx2.HTTPError):
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= _PARKED_BODY_BYTES:
                break
    await _close(response)
    return httpx2.Response(
        status_code=response.status_code,
        headers=[(k, v) for k, v in response.headers.multi_items() if k.lower() not in _WIRE_ENCODING_HEADERS],
        content=b"".join(chunks)[:_PARKED_BODY_BYTES],
        request=request,
        extensions=response.extensions,
    )


def _clone(request: httpx2.Request) -> httpx2.Request:
    """Return a copy of *request* for a hedge to send beside it: own headers and extensions, the same body stream."""
    return httpx2.Request(
        request.method,
        request.url,
        headers=request.headers.copy(),
        stream=request.stream,
        extensions=dict(request.extensions),
    )


@dataclass(frozen=True, slots=True)
class _Decision:
    """What follows a classified attempt: a retry after ``delay`` seconds, or a stop, explained by ``note`` if set."""

    delay: float | None = None
    note: str | None = None


#: The decision to stop without a note: an answer, a failure that is not retryable, or the attempts spent.
_STOP = _Decision()


@dataclass(frozen=True, slots=True)
class _Via:
    """How a sequence sends and waits: the transport below the retry layer, and the injected sleep."""

    transport: httpx2.AsyncBaseTransport
    sleep: Sleep


class _Sequence:
    """One request's attempts under one policy: the state of a single :meth:`RetryTransport.handle_async_request`."""

    def __init__(
        self,
        request: httpx2.Request,
        context: CallContext,
        policy: RetryPolicy,
        *,
        via: _Via,
        ledger: RetryLedger | None,
    ) -> None:
        """Prepare to send *request*, whose call is *context*, under *policy*, *via* the layer below.

        *ledger* is the retry budget's account, if the policy has a budget.
        """
        self._transport = via.transport
        self._sleep = via.sleep
        self._request = request
        self._context = context
        self._policy = policy
        self._timeout = request.extensions.get("timeout")
        self._method = request.method.upper()
        self._keyed = (context.idempotency_key or request.headers.get(_IDEMPOTENCY_HEADER)) is not None
        self._replayable = context.get(REPLAYABLE_BODY) is not False
        self._ledger = ledger
        self._sent = 0

    # Attempts.

    def _fit_timeout(self, request: httpx2.Request) -> None:
        """Cut the request's own timeouts to the time left before the deadline, never below ``min_attempt_time``."""
        if self._timeout is not None:
            request.extensions["timeout"] = self._timeout
        self._context.fit_timeouts(request, floor=self._policy.min_attempt_time)

    def _attempted(self, progress: SendProgress) -> Attempted:
        """Return what classification needs to know about this request and one attempt's *progress*."""
        return Attempted(method=self._method, keyed=self._keyed, progress=progress)

    def _judge(self, result: httpx2.Response | Exception) -> RetryOutcome:
        """Return this policy's verdict on *result*, another result of the call, as the current attempt produced it."""
        verdict = classify(result, self._attempted(self._context.progress), self._policy)
        return RetryOutcome(verdict.retryable, verdict.retry_after)

    async def _send(self, request: httpx2.Request, *, hedge: bool = False) -> _Attempt:
        """Send *request* once, as a new attempt, and return its classified result with its history line added."""
        context = self._context
        context.begin_attempt()
        progress = context.progress
        self._sent += 1
        number = self._sent
        self._fit_timeout(request)
        started = context.clock()
        prefix = f"attempt {number}/{self._policy.attempts}{' (hedge)' if hedge else ''}"
        result: httpx2.Response | Exception
        try:
            result = await self._transport.handle_async_request(request)
        except NETWORK_ERRORS as exc:
            result = exc
        except asyncio.CancelledError:
            # A losing hedge, or the whole call reaching its deadline: the history still accounts for the send.
            context.add_note(f"{prefix}: cancelled after {_rounded(context.clock() - started)}")
            raise
        verdict = classify(result, self._attempted(progress), self._policy)
        context.add_note(f"{prefix}: {_describe(result, verdict)} after {_rounded(context.clock() - started)}")
        return _Attempt(result, verdict, request)

    async def _round(self) -> _Attempt:
        """Run one attempt: hedged when a hedge is configured and the request is idempotent and replayable."""
        hedge = self._policy.hedge
        if hedge is None or self._method not in self._policy.methods or not self._replayable:
            return await self._send(self._request)
        return await self._hedged(hedge)

    async def _hedged(self, hedge: Hedge) -> _Attempt:
        """Send the request, adding a hedge each time ``hedge.after`` passes unanswered; return the first answer.

        A retryable failure from one send leaves the others running; once every send has failed, the last failure is
        returned to the retry loop. Every send that is not returned is cancelled, and its response closed.
        """
        sends = [asyncio.create_task(self._send(self._request))]
        pending = set(sends)
        timer: asyncio.Task[None] | None = None
        extra = 0
        failed: _Attempt | None = None
        keep: _Attempt | None = None
        try:
            while True:
                if timer is None and extra < hedge.max_extra and self._sent < self._policy.attempts:
                    timer = asyncio.create_task(_waited(self._sleep, hedge.after))
                waiting: set[asyncio.Task[_Attempt] | asyncio.Task[None]] = {*pending}
                if timer is not None:
                    waiting.add(timer)
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if timer in done:
                    timer = None
                    if self._admit_hedge(hedge.after):
                        extra += 1
                        task = asyncio.create_task(self._send(_clone(self._request), hedge=True))
                        sends.append(task)
                        pending.add(task)
                    else:
                        extra = hedge.max_extra
                for task in [send for send in sends if send in done]:
                    pending.discard(task)
                    attempt = task.result()
                    if not attempt.retryable_failure:
                        keep = attempt
                        return attempt
                    failed = attempt
                if not pending and failed is not None:
                    keep = failed
                    return failed
        finally:
            if timer is not None:
                timer.cancel()
            await _cancel_losers([*sends, *([] if timer is None else [timer])], keep=keep)

    def _admit_hedge(self, after: float) -> bool:
        """Return whether a hedge may start now (time left, breaker closed, budget), emitting ``RetryScheduled``."""
        if self._stop_reason() is not None:
            return False
        if self._ledger is not None and not self._ledger.try_spend():
            return False
        self._scheduled(after, _HEDGE_REASON)
        return True

    # Decisions.

    def _stop_reason(self) -> str | None:
        """Return why no further attempt may start now (the deadline or the breaker), or ``None``."""
        remaining = self._context.remaining()
        if remaining is not None and remaining < self._policy.min_attempt_time:
            return "deadline exhausted"
        gate = self._context.get(BREAKER_GATE)
        if gate is not None and gate():
            return "circuit opened"
        return None

    def _decide(self, attempt: _Attempt) -> _Decision:
        """Return what follows *attempt*: the wait before retrying it, or a stop with the reason it is noted for."""
        if not attempt.retryable_failure or self._sent >= self._policy.attempts:
            return _STOP
        if (note := self._unsendable(attempt)) is not None:
            return _Decision(note=note)
        delay = self._policy.wait_after(attempt.result, self._sent)
        if (note := self._unaffordable(delay)) is not None:
            return _Decision(note=note)
        return _Decision(delay=delay)

    def _unsendable(self, attempt: _Attempt) -> str | None:
        """Return why the request may not be sent again at any time: its body, or a ``Retry-After`` past the cap."""
        if not self._replayable:
            return "the request body is a one-shot stream, so it is not sent again"
        asked = attempt.verdict.retry_after
        if asked is not None and asked > self._policy.max_retry_after:
            return f"Retry-After {_rounded(asked)} exceeds max_retry_after {_rounded(self._policy.max_retry_after)}"
        return None

    def _unaffordable(self, delay: float) -> str | None:
        """Return why a retry after *delay* seconds may not be scheduled: the deadline, the breaker, the budget."""
        remaining = self._context.remaining()
        if remaining is not None and remaining - delay < self._policy.min_attempt_time:
            return "deadline exhausted"
        if (reason := self._stop_reason()) is not None:
            return reason
        if self._ledger is not None and not self._ledger.try_spend():
            return "retry budget exhausted"
        return None

    def _scheduled(self, delay: float, reason: str) -> None:
        """Emit ``RetryScheduled`` for the attempt about to be scheduled, when anyone is listening."""
        context = self._context
        if context.observed:
            context.emit(RetryScheduled(call=context.info, attempt=context.attempt + 1, delay=delay, reason=reason))

    # The sequence.

    async def run(self) -> httpx2.Response:
        """Send until an answer, or a failure the policy does not retry; return the response or raise the exception."""
        if self._ledger is not None:
            self._ledger.record_first()
        try:
            attempt = await self._round()
            while True:
                verdict = attempt.verdict
                self._context.put(RETRY_OUTCOME, RetryOutcome(verdict.retryable, verdict.retry_after, self._judge))
                decision = self._decide(attempt)
                if decision.delay is None:
                    if decision.note is not None:
                        self._context.add_note(decision.note)
                    return _settle(attempt)
                self._scheduled(decision.delay, verdict.reason)
                attempt = await _parked_attempt(attempt)
                await self._sleep(decision.delay)
                if (reason := self._stop_reason()) is not None:
                    self._context.add_note(reason)
                    return _settle(attempt)
                await _release(attempt)
                attempt = await self._round()
        finally:
            if self._timeout is not None:
                self._request.extensions["timeout"] = self._timeout


async def _waited(sleep: Sleep, seconds: float) -> None:
    """Wait *seconds* through *sleep*, as a coroutine a task can run."""
    await sleep(seconds)


def _settle(attempt: _Attempt) -> httpx2.Response:
    """Return *attempt*'s response, or raise its exception."""
    if isinstance(attempt.result, Exception):
        raise attempt.result
    return attempt.result


async def _release(attempt: _Attempt) -> None:
    """Close *attempt*'s response, if it has one: it is not the one returned."""
    if isinstance(attempt.result, httpx2.Response):
        await _close(attempt.result)


async def _parked_attempt(attempt: _Attempt) -> _Attempt:
    """Return *attempt* with its response, if any, parked in memory (see :func:`_parked`) for the wait."""
    if not isinstance(attempt.result, httpx2.Response):
        return attempt
    return _Attempt(await _parked(attempt.result, attempt.request), attempt.verdict, attempt.request)


async def _cancel_losers(tasks: list[asyncio.Task[_Attempt] | asyncio.Task[None]], *, keep: _Attempt | None) -> None:
    """Cancel every task still running, wait for them all, and close every attempt's response but *keep*'s."""
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, _Attempt) and result is not keep:
            await _release(result)


class RetryTransport(httpx2.AsyncBaseTransport):
    """Re-send a request when its policy says it is safe and worthwhile, and close what it wraps.

    See the module docstring for the sequence it runs. One :class:`~headgate.retry._budget.RetryLedger` is kept per
    distinct :class:`~headgate.retry.RetryBudget`, shared by every call through this transport (one client and its
    views).
    """

    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport,
        policy: RetryPolicy,
        sleep: Sleep = asyncio_sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        """Re-send through *transport* under *policy*, waiting with *sleep*; a budget's window runs on *clock*."""
        self._transport = transport
        self._via = _Via(transport, sleep)
        self.policy = policy
        self._clock = clock
        self._ledgers: dict[RetryBudget, RetryLedger] = {}

    def _ledger(self, budget: RetryBudget | None) -> RetryLedger | None:
        """Return the ledger this transport keeps for *budget*, created on first use; ``None`` without a budget."""
        if budget is None:
            return None
        ledger = self._ledgers.get(budget)
        if ledger is None:
            ledger = self._ledgers[budget] = RetryLedger(budget, self._clock)
        return ledger

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        context = CallContext.of(request)
        policy = context.get(RETRY_POLICY) or self.policy
        sequence = _Sequence(request, context, policy, via=self._via, ledger=self._ledger(policy.budget))
        return await sequence.run()

    @override
    async def aclose(self) -> None:
        """Close the wrapped transport (``httpx2.AsyncBaseTransport.aclose`` is a no-op)."""
        await self._transport.aclose()
