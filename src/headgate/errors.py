# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The failure taxonomy: raised by clients, caught by their callers.

The six a caller catches most, ``ClientError``, ``TransportError``, ``ClientTimeoutError``, ``UpstreamStatusError``,
``AuthError`` and ``RateLimitError``, are exported from ``headgate`` and only there, so a traceback names
``headgate.UpstreamStatusError``; every other error, ``ErrorDetails`` and ``Problem`` are exported from here.

The shape of the tree is part of the contract, as well as the names: a boundary handler can map
``ClientTimeoutError`` to 504, ``ClientConfigurationError`` to 503 and every other ``ClientError`` to 502, and
re-parenting a class changes that mapping.

:class:`DecodeError` is deliberately not a ``ClientError``. A codec raises it for a body it cannot decode, and the base
client turns it into ``ResponseParseError`` (a 2xx body) or the status error (a declared non-2xx), as it does the
``ValueError``, ``LookupError`` or ``TypeError`` a hand-written reader raises for a body of the wrong shape, or nesting
too deep to parse.

:class:`RefusedError` is deliberately not a ``ClientError`` either. A reader or a client's ``response_check`` produces
it, and the client converts it into the ``ClientError`` its ``kind`` names, so a caller of a client never sees it.

``retryable`` has one meaning on every error: sending the call again later could succeed, and is safe as the call
stands (an idempotent method, an idempotency key, or nothing of it sent). For a status or a network failure it is the
call's retry policy's verdict. The refusals the client makes before sending, :class:`CircuitOpenError` and the rate
budget's own :class:`RateLimitError` and :class:`ClientTimeoutError`, are retryable after their ``retry_after``.
:class:`ForbiddenDestinationError`, :class:`ResponseTooLargeError`, :class:`ResponseParseError`,
:class:`ClientConfigurationError` and :class:`PageLimitError` are not: sending again meets the same refusal.
``sent`` says whether any byte of the call can have reached the network, so a write that is not retryable may still
have been processed when it is ``True``.

Every ``ClientError`` pickles, so it can cross a process or a durable-workflow boundary. The default exception reduce,
``(cls, self.args)``, re-runs ``__init__``: that fails on a required keyword-only argument (``status_code``) and would
prefix the message a second time. :meth:`ClientError.__reduce__` rebuilds the instance without ``__init__`` instead.
"""

from headgate._core import rehome as _rehome
from headgate._errors import (
    CircuitOpenError,
    ClientConfigurationError,
    DecodeError,
    ErrorDetails,
    ForbiddenDestinationError,
    PageLimitError,
    Problem,
    RefusedError,
    ResponseParseError,
    ResponseTooLargeError,
)

__all__ = [
    "CircuitOpenError",
    "ClientConfigurationError",
    "DecodeError",
    "ErrorDetails",
    "ForbiddenDestinationError",
    "PageLimitError",
    "Problem",
    "RefusedError",
    "ResponseParseError",
    "ResponseTooLargeError",
]

_rehome(__name__, globals())
