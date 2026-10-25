# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The origin helpers: normalised comparison and the ``scheme://host[:port]`` text."""

from __future__ import annotations

import httpx2
import pytest

from headgate._core import origin_of, origin_text, same_origin


@pytest.mark.parametrize(
    ("url", "text"),
    [
        ("https://API.Example.test/x?y=1", "https://api.example.test"),
        ("https://api.example.test:443/", "https://api.example.test"),
        ("http://api.example.test:8080", "http://api.example.test:8080"),
        ("https://[::1]:8443/x", "https://[::1]:8443"),
    ],
)
def test_origin_text_is_normalised_and_drops_the_default_port(url: str, text: str) -> None:
    """Lower-case the host, drop the scheme's default port, and bracket an IPv6 host."""
    assert origin_text(origin_of(url)) == text


def test_same_origin_compares_scheme_host_and_effective_port() -> None:
    """Treat an explicit default port as the same origin, and another scheme or port as a different one."""
    assert same_origin("https://a.test/x", httpx2.URL("https://a.test:443/y"))
    assert not same_origin("https://a.test", "http://a.test")
    assert not same_origin("https://a.test", "https://a.test:8443")


def test_a_relative_url_has_no_origin() -> None:
    """Refuse a relative URL, which addresses no origin."""
    with pytest.raises(ValueError, match="absolute"):
        origin_of("/v1/x")
