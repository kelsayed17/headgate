"""``sanitize_url``: what every ``ClientError`` message does to a URL before naming it."""

from __future__ import annotations

from headgate._url import sanitize_url


def test_a_url_without_a_query_is_returned_as_is() -> None:
    assert sanitize_url("series/observations") == "series/observations"


def test_a_secret_parameter_is_redacted_and_the_others_are_kept() -> None:
    assert (
        sanitize_url("https://api.x/obs?series_id=GDP&api_key=abc")
        == "https://api.x/obs?series_id=GDP&api_key=%2A%2A%2A"
    )
