# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Retry: when a failed call is re-sent, and the layers that re-send it and record what each send did.

* :class:`RetryPolicy` and its values :class:`Backoff`, :class:`RetryBudget` and :class:`Hedge` configure it;
  :data:`NO_RETRY` sends once.
* :class:`RetryTransport` runs each request's attempts; :class:`SendTrackingTransport`, directly above the network,
  records whether each attempt connected and how many request bytes it wrote, so a write that may have been processed
  is never repeated unless that is safe.
* :data:`REPLAYABLE_BODY` is the call-context slot a layer sets to ``False`` for a body that cannot be sent twice.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate.retry._policy import (
    IDEMPOTENT_METHODS,
    NO_RETRY,
    REPLAYABLE_BODY,
    RETRY_STATUSES,
    Backoff,
    Hedge,
    RetryBudget,
    RetryPolicy,
)
from headgate.retry._tracking import SendTrackingTransport
from headgate.retry._transport import RetryTransport

__all__ = [
    "IDEMPOTENT_METHODS",
    "NO_RETRY",
    "REPLAYABLE_BODY",
    "RETRY_STATUSES",
    "Backoff",
    "Hedge",
    "RetryBudget",
    "RetryPolicy",
    "RetryTransport",
    "SendTrackingTransport",
]
