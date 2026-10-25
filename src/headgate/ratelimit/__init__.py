# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Rate limiting: a client's budget (:class:`Rate`, :class:`RateRule`) and the layer that enforces it.

Self-contained: it imports only ``httpx2``, ``aiolimiter``, the standard library and :mod:`headgate._core`.
"""

from headgate.ratelimit._limiter import AsyncRateLimiter, PathLimiters
from headgate.ratelimit._rate import UNLIMITED, Rate, RateRule
from headgate.ratelimit._transport import RateLimitTransport

__all__ = ["UNLIMITED", "AsyncRateLimiter", "PathLimiters", "Rate", "RateLimitTransport", "RateRule"]
