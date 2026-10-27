# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Retry: when a failed call is re-sent.

:class:`RetryPolicy` and its values :class:`Backoff`, :class:`RetryBudget` and :class:`Hedge` configure it;
:data:`NO_RETRY` sends once. :data:`IDEMPOTENT_METHODS` and :data:`RETRY_STATUSES` are the policy's defaults, to build
on. The layers that re-send a request and record what each send did are the client's own, in this package's private
modules.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate.retry._policy import (
    IDEMPOTENT_METHODS,
    NO_RETRY,
    RETRY_STATUSES,
    Backoff,
    Hedge,
    RetryBudget,
    RetryPolicy,
)

__all__ = ["IDEMPOTENT_METHODS", "NO_RETRY", "RETRY_STATUSES", "Backoff", "Hedge", "RetryBudget", "RetryPolicy"]
