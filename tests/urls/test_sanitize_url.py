"""``sanitize_url``: what every ``ClientError`` message does to a URL before naming it."""

from __future__ import annotations

import pytest

from headgate._url import sanitize_url


def test_a_url_without_a_query_is_returned_as_is() -> None:
    assert sanitize_url("series/observations") == "series/observations"


def test_a_secret_parameter_is_redacted_and_the_others_are_kept() -> None:
    assert (
        sanitize_url("https://api.x/obs?series_id=GDP&api_key=abc")
        == "https://api.x/obs?series_id=GDP&api_key=%2A%2A%2A"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:hunter2@api.example.test/x", "https://user:***@api.example.test/x"),
        (
            "https://api.example.test/cb#access_token=abc&state=s",
            "https://api.example.test/cb#access_token=***&state=s",
        ),
        (
            "https://bucket.example.test/f?X-Amz-Signature=abc&x=1",
            "https://bucket.example.test/f?X-Amz-Signature=%2A%2A%2A&x=1",
        ),
        ("https://api.example.test/token?refresh_token=abc", "https://api.example.test/token?refresh_token=%2A%2A%2A"),
        ("https://user@api.example.test/x", "https://user@api.example.test/x"),
    ],
)
def test_credentials_outside_known_query_names_are_redacted(url: str, expected: str) -> None:
    assert sanitize_url(url) == expected
