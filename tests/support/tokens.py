# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Pre-mint a ``CachedTokenAuth`` so an endpoint test makes no token exchange.

Seeding the cache with a token that never expires keeps a client's endpoint suites about the endpoint, not about the
exchange. A seeded token's age is unknown, so a 401 on it re-mints, as a 401 on a token held for minutes does in
production (a token minted within ``FRESH_TOKEN_S`` is not re-minted on a 401).
"""

from __future__ import annotations

import math

from headgate._auth import CachedTokenAuth

#: The bearer every pre-minted auth stamps, for a test that asserts on the header.
TEST_BEARER = "test-token"


def premint[A: CachedTokenAuth](auth: A, token: str = TEST_BEARER) -> A:
    """Return *auth* with *token* cached and never expiring, so it stamps without minting."""
    auth.seed(token, math.inf)
    return auth
