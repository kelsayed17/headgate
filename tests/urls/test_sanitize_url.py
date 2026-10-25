# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``sanitize_url`` and ``redact_secrets``: what every ``ClientError`` message does to a URL before naming it."""

from __future__ import annotations

import uuid

import pytest

from headgate._url import is_secret_param, redact_secrets, register_secret_params, sanitize_url


def test_a_url_without_a_query_is_returned_as_is() -> None:
    """Return a URL with no query, fragment or userinfo unchanged."""
    assert sanitize_url("series/observations") == "series/observations"


def test_a_secret_parameter_is_redacted_and_the_others_are_kept() -> None:
    """Replace a secret parameter's value and keep the others."""
    assert (
        sanitize_url("https://api.x/obs?series_id=GDP&api_key=abc")
        == "https://api.x/obs?series_id=GDP&api_key=%2A%2A%2A"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:hunter2@api.example.test/x", "https://user:***@api.example.test/x"),
        ("https://user:p@ss@api.example.test/x", "https://user:***@api.example.test/x"),
        (
            "https://api.example.test/cb#access_token=abc&state=s",
            "https://api.example.test/cb#access_token=***&state=s",
        ),
        (
            "https://bucket.example.test/f?X-Amz-Signature=abc&x=1",
            "https://bucket.example.test/f?X-Amz-Signature=%2A%2A%2A&x=1",
        ),
        ("https://api.example.test/token?refresh_token=abc", "https://api.example.test/token?refresh_token=%2A%2A%2A"),
        ("https://api.example.test/x?api%5Fkey=abc", "https://api.example.test/x?api%5Fkey=%2A%2A%2A"),
        ("https://user@api.example.test/x", "https://user@api.example.test/x"),
        ("https://user:@api.example.test/x", "https://user:@api.example.test/x"),
    ],
)
def test_credentials_outside_known_query_names_are_redacted(url: str, expected: str) -> None:
    """Redact a userinfo password, a fragment credential, and a percent-encoded parameter name."""
    assert sanitize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.test/x?a=1&b=2&a=3",
        "https://api.example.test/x?flag&q=a%20b+c",
        "https://api.example.test/x?raw=%FF",
        "https://api.example.test/x;params?q=1",
    ],
)
def test_a_url_with_no_credential_is_kept_byte_for_byte(url: str) -> None:
    """Keep repeated names in order, a bare flag, the original encoding, and path params.

    The URL a message names is the one that was sent; decoding and re-encoding the query would reorder repeated names,
    turn ``flag`` into ``flag=``, ``%20`` into ``+`` and an invalid ``%FF`` into a replacement character.
    """
    assert sanitize_url(url) == url


def test_only_the_secret_value_changes_among_repeated_names() -> None:
    """Redact the secret in place, leaving the parameters around it where they were."""
    assert sanitize_url("https://x.test/?a=1&key=k&a=3") == "https://x.test/?a=1&key=%2A%2A%2A&a=3"


def test_redact_secrets_replaces_secret_pairs_in_free_text() -> None:
    """Redact a ``name=value`` pair in a message, stopping the value at a quote or whitespace."""
    text = "bad response from 'https://x.test/?apikey=abc&q=1' and token=def end"
    assert redact_secrets(text) == "bad response from 'https://x.test/?apikey=***&q=1' and token=*** end"


def test_a_registered_name_is_secret_whatever_its_spelling() -> None:
    """Fold a registered name like a listed one: case-insensitive, with ``_`` and ``-`` ignored."""
    # The registry is process-wide and add-only, so a name unique to this run keeps the test independent of order.
    unique = f"zz{uuid.uuid4().hex}"
    assert not is_secret_param(f"{unique}-registered")
    register_secret_params([f"{unique.upper()}_Registered"])
    assert is_secret_param(f"{unique}-registered")
    redacted = sanitize_url(f"https://x.test/?{unique}registered=v")
    assert redacted == f"https://x.test/?{unique}registered=%2A%2A%2A"
