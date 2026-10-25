# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Retry: the policy a call is re-sent under (:class:`RetryPolicy`, :class:`Backoff`) and the layer that re-sends it.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate.retry._policy import NO_RETRY, Backoff, RetryPolicy
from headgate.retry._transport import RetryTransport

__all__ = ["NO_RETRY", "Backoff", "RetryPolicy", "RetryTransport"]
