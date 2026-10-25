# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""URL escaping and credential redaction hold for any input, not only the examples the unit tests chose."""

from __future__ import annotations

from urllib.parse import unquote

import httpx2
from hypothesis import assume, given
from hypothesis import strategies as st

from headgate._url import SECRET_QUERY_PARAMS, escape_segment, redact_secrets, render_path, sanitize_url

#: Any text a caller could interpolate into a path: every code point except lone surrogates, which are not text.
_VALUES = st.text(alphabet=st.characters(exclude_categories=["Cs"]), min_size=1)
#: A credential value: long and random enough that it cannot appear elsewhere in the URL by chance, and drawn from the
#: characters real tokens use, ``=`` padding included, since a base64 token commonly ends in ``==``.
_CREDENTIALS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_.~=", min_size=16, max_size=40).filter(
    lambda value: any(char.isalnum() for char in value)
)
_SECRET_NAMES = st.sampled_from(sorted(SECRET_QUERY_PARAMS))
_STRUCTURAL = ("/", "?", "#")


@given(_VALUES)
def test_an_escaped_segment_is_one_segment_that_decodes_back_to_the_value(value: str) -> None:
    """Escape any value into exactly one segment that cannot be structure, and that decodes back to the value."""
    escaped = escape_segment(value)

    assert not any(char in escaped for char in _STRUCTURAL)
    assert escaped not in {"", ".", ".."}
    assert unquote(escaped) == value


@given(_VALUES, _VALUES)
def test_a_rendered_path_has_one_segment_per_interpolation(account: str, symbol: str) -> None:
    """Keep each interpolated value in its own segment after httpx2 parses the URL, dot segments included."""
    url = httpx2.URL("https://api.example.test" + render_path(t"/v2/accounts/{account}/positions/{symbol}"))

    segments = url.raw_path.decode("ascii").split("/")

    assert [unquote(segment) for segment in segments] == ["", "v2", "accounts", account, "positions", symbol]


@given(_SECRET_NAMES, _CREDENTIALS)
def test_a_secret_query_value_never_survives_sanitizing(name: str, credential: str) -> None:
    """Remove the value of every secret parameter from a URL, wherever the parameter sits in the query."""
    assume(credential not in name)
    url = f"https://api.example.test/v1/series?id=GDP&{name}={credential}&limit=10"

    assert credential not in sanitize_url(url)


@given(_CREDENTIALS, _CREDENTIALS)
def test_a_userinfo_password_never_survives_sanitizing(user: str, password: str) -> None:
    """Remove the password from a URL's userinfo while keeping the rest of the URL."""
    assume(password not in user)
    sanitized = sanitize_url(f"https://{user}:{password}@api.example.test/v1/series")

    assert password not in sanitized
    assert sanitized.endswith("api.example.test/v1/series")


@given(_SECRET_NAMES, _CREDENTIALS, st.text(alphabet=" .:;,", min_size=1, max_size=5))
def test_a_secret_pair_in_free_text_never_survives_redaction(name: str, credential: str, separator: str) -> None:
    """Remove a secret ``name=value`` pair's value from an error message, whatever non-word text surrounds it."""
    message = f"GET https://api.example.test/x failed{separator}{name}={credential}{separator}retrying"

    assert credential not in redact_secrets(message)


@given(st.text(alphabet=st.characters(exclude_categories=["Cs"])))
def test_a_url_without_credentials_is_returned_unchanged(path: str) -> None:
    """Leave a URL with no query, fragment or userinfo exactly as it was."""
    url = "https://api.example.test/" + "".join(char for char in path if char not in "?#@")

    assert sanitize_url(url) == url
