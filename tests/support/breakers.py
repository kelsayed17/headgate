# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Read the shared default breaker registry from a test."""

from __future__ import annotations

import httpx2

from headgate.breaker._registry import DEFAULT_REGISTRY


def failures(host: str) -> int:
    """Return the consecutive failures the default registry's breakers for *host* hold, on any scheme or port."""
    return sum(breaker.failure_count for breaker in DEFAULT_REGISTRY if httpx2.URL(breaker.name).host == host)


def has_breaker(host: str) -> bool:
    """Return whether the default registry holds a breaker for *host*, on any scheme or port."""
    return any(httpx2.URL(breaker.name).host == host for breaker in DEFAULT_REGISTRY)
