# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Read the shared default breaker registry from a test."""

from __future__ import annotations

from headgate.breaker import DEFAULT_REGISTRY


def failures(host: str) -> int:
    """Return how many consecutive failures *host*'s default-registry breaker holds; 0 when it has none yet."""
    return DEFAULT_REGISTRY[host].failure_count if host in DEFAULT_REGISTRY else 0
