# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Classify one attempt's result: an answer to return, or a failure, and whether re-sending it is safe.

The rules, in order:

* A response whose status is not in the policy's ``statuses`` is an answer (unless ``x-should-retry: true`` says
  otherwise). Only network failures (:data:`~headgate.retry._policy.NETWORK_ERRORS`) are classified at all; anything
  else propagates at once, and a local mistake (:data:`~headgate.retry._policy.LOCAL_ERRORS`) is never retried.
* ``x-should-retry: true|false`` decides when the policy respects it: the server knows whether a re-send is safe.
* A failure that proves the request was not processed is retryable for every method: 425 Too Early (RFC 8470), and an
  HTTP/2 stream the server reset with ``REFUSED_STREAM`` (RFC 9113 §8.7).
* An idempotent method (the policy's ``methods``), or a request carrying an idempotency key, is retryable.
* Any other request is retryable only when nothing of it was written. That is decided from the attempt's
  :class:`~headgate._core.SendProgress` when a layer reported it (a connection obtained and zero bytes written), and
  otherwise from the exception's type (:data:`~headgate.retry._policy.NEVER_SENT_ERRORS`).

**HTTP/2 detection.** httpcore2 raises ``RemoteProtocolError`` with the h2 ``StreamReset`` event as its argument, and
httpx2 re-raises it as its own ``RemoteProtocolError`` chained from that, so the error code is read from the cause
chain (by duck typing: this package does not import h2). A GOAWAY whose last stream id is below the request's stream is
handled inside httpcore2, which moves the request to a new connection itself, so it never reaches this layer; a GOAWAY
that covers the request's stream may have been processed and follows the ordinary rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import httpx2

from headgate._core import retry_after_of
from headgate.retry._policy import LOCAL_ERRORS, NEVER_SENT_ERRORS

if TYPE_CHECKING:
    from headgate._core import SendProgress
    from headgate.retry._policy import RetryPolicy

__all__ = ["Attempted", "Verdict", "classify", "refused_stream"]

#: The HTTP/2 error code a server resets a stream with when it did no processing on it (RFC 9113 §7).
_REFUSED_STREAM: Final = 0x7

#: How far down an exception's cause chain the stream reset is looked for.
_CAUSE_DEPTH: Final = 4

#: The status RFC 8470 defines for a request the server will not process yet: it was not processed.
_TOO_EARLY: Final = 425


@dataclass(frozen=True, slots=True)
class Verdict:
    """What an attempt's result is: a ``failure`` or an answer, whether it is ``retryable``, and why, for events."""

    failure: bool
    retryable: bool
    reason: str
    retry_after: float | None = None


#: The verdict on an answer: nothing to retry.
_ANSWER = Verdict(failure=False, retryable=False, reason="answered")


def refused_stream(exc: BaseException) -> bool:
    """Return whether *exc* is an HTTP/2 stream reset with ``REFUSED_STREAM``, looking down its cause chain."""
    current: BaseException | None = exc
    for _ in range(_CAUSE_DEPTH):
        if current is None:
            return False
        for arg in current.args:
            if type(arg).__name__ == "StreamReset" and getattr(arg, "error_code", None) == _REFUSED_STREAM:
                return True
        current = current.__cause__
    return False


@dataclass(frozen=True, slots=True)
class Attempted:
    """What the request was, for classification: its method, whether it carries an idempotency key, and its progress."""

    method: str
    keyed: bool
    progress: SendProgress


def _nothing_sent(progress: SendProgress, exc: Exception) -> bool:
    """Return whether no byte of the request was written: from the reported progress, else from the exception type."""
    if progress.bytes_sent:
        return False
    if progress.connected:
        return True
    return isinstance(exc, NEVER_SENT_ERRORS)


def _should_retry(response: httpx2.Response, policy: RetryPolicy) -> bool | None:
    """Return the server's ``x-should-retry`` answer, or ``None`` when it gave none or the policy ignores it."""
    if not policy.respect_should_retry:
        return None
    header = response.headers.get("x-should-retry", "").strip().lower()
    return {"true": True, "false": False}.get(header)


def _classify_response(response: httpx2.Response, attempted: Attempted, policy: RetryPolicy) -> Verdict:
    """Classify a response by its status, the server's ``x-should-retry``, and the method's safety."""
    status = response.status_code
    should = _should_retry(response, policy)
    if status not in policy.statuses and should is not True:
        return _ANSWER
    retry_after = retry_after_of(response)
    reason = str(status)
    if should is not None:
        return Verdict(failure=True, retryable=should, reason=reason, retry_after=retry_after)
    safe = status == _TOO_EARLY or attempted.method in policy.methods or attempted.keyed
    return Verdict(failure=True, retryable=safe, reason=reason, retry_after=retry_after)


def _classify_error(exc: Exception, attempted: Attempted, policy: RetryPolicy) -> Verdict:
    """Classify a network failure: a local mistake never, a refused stream always, otherwise by safety."""
    reason = type(exc).__name__
    if isinstance(exc, LOCAL_ERRORS):
        return Verdict(failure=True, retryable=False, reason=reason)
    if refused_stream(exc):
        return Verdict(failure=True, retryable=True, reason="REFUSED_STREAM")
    safe = attempted.method in policy.methods or attempted.keyed or _nothing_sent(attempted.progress, exc)
    return Verdict(failure=True, retryable=safe, reason=reason)


def classify(result: httpx2.Response | Exception, attempted: Attempted, policy: RetryPolicy) -> Verdict:
    """Return the verdict on *result*, one attempt of the request *attempted* under *policy*."""
    if isinstance(result, httpx2.Response):
        return _classify_response(result, attempted, policy)
    return _classify_error(result, attempted, policy)
