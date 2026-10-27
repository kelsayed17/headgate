# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a response tells the rate budget: pushback and pause, from ``Retry-After`` and the rate-limit header fields.

The IETF fields follow draft-ietf-httpapi-ratelimit-headers-11 (Structured Field Lists; ``r`` the quota left, ``t`` the
seconds until it returns, ``qu`` a policy's unit); the de facto ``X-RateLimit-*`` pair carries a reset in seconds or as
a Unix time.
"""

from __future__ import annotations

import email.utils

import httpx2
import pytest

from headgate.ratelimit._feedback import Feedback, read_feedback
from headgate.ratelimit._fields import Item, parse_list

#: The wall clock every test reads, as a Unix time (2023-11-14T22:13:20Z).
_NOW = 1_700_000_000.0

#: A reset some seconds after :data:`_NOW`.
_RESET_IN = 42.0


def _feedback(status: int = 200, **headers: str) -> Feedback:
    """Return the feedback of a response with *status* and *headers* (underscores in names become dashes)."""
    named = {name.replace("_", "-"): value for name, value in headers.items()}
    return read_feedback(httpx2.Response(status, headers=named), now=_NOW)


# --- Retry-After ---


def test_a_429_or_503_is_pushback_and_its_retry_after_the_pause() -> None:
    """Read pushback from a 429 or 503, and the pause from its ``Retry-After``."""
    assert _feedback(429, Retry_After="3") == Feedback(pushback=True, pause=3.0)
    assert _feedback(503) == Feedback(pushback=True, pause=None)


def test_a_retry_after_on_another_status_is_not_a_pause() -> None:
    """Ignore ``Retry-After`` on a status that is not a refusal (a redirect, say)."""
    assert _feedback(301, Retry_After="3") == Feedback()


# --- RateLimit (IETF) ---


def test_an_exhausted_policy_pauses_for_its_window() -> None:
    """Pause for ``t`` when a policy has no quota left, even on a 200."""
    assert _feedback(RateLimit='"default";r=0;t=30') == Feedback(pushback=True, pause=30.0)


def test_quota_left_says_nothing() -> None:
    """Treat a policy with quota left as no feedback."""
    assert _feedback(RateLimit='"default";r=50;t=30', RateLimit_Policy='"default";q=100;w=60') == Feedback()


def test_the_longest_window_among_exhausted_policies_wins() -> None:
    """Pause until every exhausted policy has quota again."""
    field = '"burst";r=0;t=5, "daily";r=0;t=3600, "hourly";r=9;t=60'
    assert _feedback(RateLimit=field) == Feedback(pushback=True, pause=3600.0)


def test_an_exhausted_policy_without_a_window_is_pushback_without_a_pause() -> None:
    """Throttle on an exhausted policy that does not say when quota returns, without guessing a pause."""
    assert _feedback(RateLimit='"default";r=0') == Feedback(pushback=True, pause=None)


def test_a_policy_counting_something_other_than_requests_is_skipped() -> None:
    """Skip a policy whose quota unit is bytes or concurrency: its quota is not the request rate."""
    policies = '"bytes";q=1000;qu="content-bytes";w=10, "requests";q=10;w=10'
    field = '"bytes";r=0;t=10, "requests";r=4;t=10'
    assert _feedback(RateLimit=field, RateLimit_Policy=policies) == Feedback()


def test_retry_after_takes_precedence_over_the_window() -> None:
    """Pause for ``Retry-After`` rather than ``t`` when a refusal carries both, as the draft says."""
    assert _feedback(429, Retry_After="2", RateLimit='"default";r=0;t=30') == Feedback(pushback=True, pause=2.0)


@pytest.mark.parametrize(
    "field",
    ['"default";r=-1;t=5', '"default";r=?0', '"default";r=0.0;t=5', '"default";t=5', '"default";r=0;t=-5'],
)
def test_a_remaining_or_window_that_is_not_a_count_is_not_read(field: str) -> None:
    """Read ``r`` and ``t`` only as non-negative Integers; a Boolean, a Decimal or a negative is not one."""
    feedback = _feedback(RateLimit=field)
    assert feedback.pause is None


def test_a_malformed_ratelimit_field_falls_back_to_the_de_facto_headers() -> None:
    """Ignore a malformed ``RateLimit`` and read ``X-RateLimit-*`` instead."""
    feedback = _feedback(RateLimit='"default";r=0;t=5,', X_RateLimit_Remaining="0", X_RateLimit_Reset="9")
    assert feedback == Feedback(pushback=True, pause=9.0)


# --- X-RateLimit-Remaining / X-RateLimit-Reset ---


@pytest.mark.parametrize(
    ("reset", "pause"),
    [
        (str(int(_RESET_IN)), _RESET_IN),
        (str(int(_NOW + _RESET_IN)), _RESET_IN),
        (str(int((_NOW + _RESET_IN) * 1000)), _RESET_IN),
        (str(int(_NOW - _RESET_IN)), 0.0),
    ],
)
def test_a_de_facto_reset_is_seconds_or_a_unix_time(reset: str, pause: float) -> None:
    """Tell a delay from a Unix time in seconds or milliseconds by size; a reset in the past is no wait."""
    assert _feedback(X_RateLimit_Remaining="0", X_RateLimit_Reset=reset) == Feedback(pushback=True, pause=pause)


def test_de_facto_headers_with_quota_left_or_garbage_say_nothing() -> None:
    """Read nothing from quota left, from unparsable values, or from no remaining count at all."""
    assert _feedback(X_RateLimit_Remaining="3", X_RateLimit_Reset="9") == Feedback()
    assert _feedback(X_RateLimit_Remaining="none") == Feedback()
    assert _feedback(X_RateLimit_Reset="9") == Feedback()


def test_de_facto_lists_take_the_most_restrictive_value() -> None:
    """Read a repeated header (joined with commas) as its smallest remaining count and latest reset."""
    feedback = _feedback(X_RateLimit_Remaining="5, 0, inf", X_RateLimit_Reset="3, 8, nope, -1")
    assert feedback == Feedback(pushback=True, pause=8.0)


def test_spent_de_facto_quota_without_a_reset_is_pushback_without_a_pause() -> None:
    """Throttle on a spent quota that names no reset."""
    assert _feedback(X_RateLimit_Remaining="0") == Feedback(pushback=True, pause=None)


# --- Structured Field Lists ---


def test_a_list_of_items_with_parameters_parses() -> None:
    """Parse every bare-item type a rate-limit field can carry, with its parameters."""
    field = '"a";q=10;w=60;pk=:aGk=:, tok;flag;neg=-3;dec=1.5;off=?0;on=?1;s="x\\"y\\\\z"'
    assert parse_list(field) == [
        Item("a", {"q": 10, "w": 60, "pk": b"hi"}),
        Item("tok", {"flag": True, "neg": -3, "dec": 1.5, "off": False, "on": True, "s": 'x"y\\z'}),
    ]


def test_the_limits_of_each_bare_item_are_inclusive() -> None:
    """Accept a 15-digit Integer, a Decimal of 12 and 3 digits, and every printable character in a String."""
    field = ' Xa;i=123456789012345;d=123456789012.123;s="a b~" '
    assert parse_list(field) == [Item("Xa", {"i": 123456789012345, "d": 123456789012.123, "s": "a b~"})]


def test_an_empty_field_is_an_empty_list() -> None:
    """Parse an absent field (an empty value) as no items."""
    assert parse_list("") == []
    assert parse_list("   ") == []


@pytest.mark.parametrize(
    "field",
    [
        '"a",',  # a trailing comma
        '"a" "b"',  # members not separated by a comma
        '("a" "b")',  # an inner list
        '"a";Q=1',  # a key that is not lowercase
        '"a";q=?2',  # a Boolean that is neither 0 nor 1
        '"a";q=-',  # a sign with no digits
        '"a";q=1234567890123456',  # an Integer past 15 digits
        '"a";q=1.2345',  # a Decimal with more than 3 fraction digits
        '"a";q=1.',  # a Decimal with no fraction
        '"a";q=1234567890123.5',  # a Decimal past 12 integer digits
        '"unterminated',
        '"bad \\n escape"',
        '"tab\tinside"',
        '"a";pk=:not base64!:',
        '"a";pk=:aGk',  # an unterminated Byte Sequence
        '"a";pk=:a:',  # base64 with bad padding
        '"a";pk=:YQ==YQ==:',  # base64 with data after its padding
        '\t"a"',  # a leading tab: only spaces surround a field
        '"a";Xb=1',  # a parameter key after a stray character
        "@",
    ],
)
def test_a_malformed_list_is_refused_whole(field: str) -> None:
    """Refuse a field that breaks the grammar anywhere, so a half-read field is never acted on."""
    assert parse_list(field) is None


# --- Edges ---


def test_an_http_date_retry_after_is_measured_from_the_wall_clock_given() -> None:
    """Measure an HTTP-date ``Retry-After`` from the *now* the budget passes, not the machine's clock."""
    later = email.utils.formatdate(_NOW + _RESET_IN, usegmt=True)

    assert _feedback(429, Retry_After=later) == Feedback(pushback=True, pause=_RESET_IN)


def test_a_readable_ratelimit_field_with_quota_left_settles_it_without_the_de_facto_pair() -> None:
    """Take a readable ``RateLimit`` with quota left as the answer, ignoring an ``X-RateLimit-Remaining: 0``."""
    assert _feedback(RateLimit='"p";r=5;t=1', X_RateLimit_Remaining="0") == Feedback()


def test_every_ratelimit_item_is_read_and_the_longest_wait_wins() -> None:
    """Read past an item with quota left, and wait for the exhausted item that returns last."""
    field = '"a";r=5;t=1, "b";r=0;t=9, "c";r=0;t=3'

    assert _feedback(RateLimit=field) == Feedback(pushback=True, pause=9.0)


def test_a_policy_without_a_unit_limits_requests() -> None:
    """Read a ``RateLimit-Policy`` naming no ``qu`` as limiting requests, the draft's default."""
    assert _feedback(RateLimit='"p";r=0;t=5', RateLimit_Policy='"p";q=100') == Feedback(pushback=True, pause=5.0)


@pytest.mark.parametrize(("remaining", "spent"), [("1", False), ("0", True), ("-1", False), ("x, 0", True)])
def test_the_de_facto_remaining_count_is_spent_only_at_zero(remaining: str, *, spent: bool) -> None:
    """Count the quota spent at zero only, skipping a part that is not a non-negative number."""
    assert _feedback(X_RateLimit_Remaining=remaining).pushback is spent


@pytest.mark.parametrize("reset", ["1000000000", "1000000000000"], ids=["seconds", "milliseconds"])
def test_a_reset_exactly_at_the_epoch_floor_is_a_unix_time(reset: str) -> None:
    """Read a reset exactly at the floor as a Unix time (seconds, or milliseconds), not a delay."""
    now = 999_999_990.0
    feedback = read_feedback(
        httpx2.Response(200, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}), now=now
    )

    assert feedback == Feedback(pushback=True, pause=10.0)
