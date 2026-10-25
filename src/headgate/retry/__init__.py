# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Retry: the policy a call is re-sent under (:class:`RetryPolicy`, :class:`Backoff`) and the layer that re-sends it.

Self-contained: it imports only ``httpx2``, the standard library and :mod:`headgate._core`.
"""

from headgate._core import Slot
from headgate.retry._policy import NO_RETRY, Backoff, RetryPolicy
from headgate.retry._transport import RetryTransport

__all__ = ["NO_RETRY", "REPLAYABLE_BODY", "Backoff", "RetryPolicy", "RetryTransport"]

#: Whether the call's request body can be sent again; ``False`` for a one-shot streamed body. Absent means replayable.
REPLAYABLE_BODY = Slot[bool]("replayable_body")
