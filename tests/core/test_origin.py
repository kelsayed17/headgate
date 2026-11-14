# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The origin helpers: normalized comparison and the ``scheme://host[:port]`` text."""

from __future__ import annotations

import pytest

from headgate._core import origin_of, origin_text


@pytest.mark.parametrize(
    ("url", "text"),
    [
        ("https://API.Example.test/x?y=1", "https://api.example.test"),
        ("https://api.example.test:443/", "https://api.example.test"),
        ("http://api.example.test:8080", "http://api.example.test:8080"),
        ("https://[::1]:8443/x", "https://[::1]:8443"),
    ],
)
def test_origin_text_is_normalized_and_drops_the_default_port(url: str, text: str) -> None:
    """Lower-case the host, drop the scheme's default port, and bracket an IPv6 host."""
    assert origin_text(origin_of(url)) == text


def test_a_relative_url_has_no_origin() -> None:
    """Refuse a relative URL, which addresses no origin."""
    with pytest.raises(ValueError, match="absolute"):
        origin_of("/v1/x")
