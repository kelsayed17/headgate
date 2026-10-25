# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Retry timing, body preparation and response declarations hold their contracts for any input."""

from __future__ import annotations

import math

import httpx2
from hypothesis import given
from hypothesis import strategies as st

from headgate import Expect, nothing
from headgate._codec import drop_none
from headgate._retry import Retry

#: The longest any single retry wait may be, as documented on ``Retry.sleep_seconds``.
_MAX_WAIT = 5.0
#: Statuses no call may declare as an answer: the client's own credentials, the rate limit, and every 5xx.
_UNCLAIMABLE = frozenset({httpx2.codes.UNAUTHORIZED, httpx2.codes.PROXY_AUTHENTICATION_REQUIRED, 429})
_LOWEST_STATUS, _HIGHEST_STATUS = 100, 599

_JSON_LEAVES = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text()
_JSON = st.recursive(
    _JSON_LEAVES,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=5), children, max_size=4),
    max_leaves=20,
)


@given(
    retry_after=st.text(alphabet=st.characters(max_codepoint=0xFF), max_size=40),
    attempts_made=st.integers(min_value=1, max_value=2000),
    backoff_factor=st.floats(min_value=0, max_value=1e6),
    jitter=st.floats(min_value=0, max_value=1),
)
def test_a_retry_wait_is_always_finite_non_negative_and_capped(
    retry_after: str, attempts_made: int, backoff_factor: float, jitter: float
) -> None:
    """Wait between zero and the cap after any response, whatever its ``Retry-After`` says and however many retries."""
    policy = Retry(
        total=attempts_made, backoff_factor=backoff_factor, attempts_made=attempts_made, jitter=lambda: jitter
    )
    # Header bytes arrive from the wire as Latin-1, so any of the 256 byte values can appear.
    headers = [(b"Retry-After", retry_after.encode("latin-1"))]
    response = httpx2.Response(httpx2.codes.SERVICE_UNAVAILABLE, headers=headers)

    wait = policy.sleep_seconds(response)

    assert math.isfinite(wait)
    assert 0 <= wait <= _MAX_WAIT


def _contains_none_value(value: object) -> bool:
    """Report whether a dict anywhere inside *value* still maps a key to ``None``."""
    if isinstance(value, dict):
        return any(item is None or _contains_none_value(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_none_value(item) for item in value)
    return False


@given(_JSON)
def test_drop_none_leaves_no_none_valued_key_and_is_idempotent(body: object) -> None:
    """Remove every ``None``-valued key at any depth, and change nothing more when applied twice."""
    dropped = drop_none(body)

    assert not _contains_none_value(dropped)
    assert drop_none(dropped) == dropped


@given(st.integers(min_value=-1000, max_value=2000))
def test_a_status_is_declarable_exactly_when_it_is_a_documented_answer(status: int) -> None:
    """Accept a declared status only inside 100-599, below 500, and not a credential or rate-limit status."""
    declarable = _LOWEST_STATUS <= status < httpx2.codes.INTERNAL_SERVER_ERROR and status not in _UNCLAIMABLE
    try:
        declared = Expect(nothing).on(status, nothing)
    except ValueError:
        assert not declarable
    else:
        assert declarable
        assert status in declared.answers
