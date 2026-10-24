"""Caller-supplied path segments are URL-escaped before they reach the wire.

A client that interpolates a caller's value straight into an f-string path
lets that value control the URL's STRUCTURE, not just one segment of it:

    f"/v2/positions/{symbol}"   with symbol="../../v2/account"
      -> httpx2 resolves the dot segments
      -> DELETE https://api.example.test/v2/account

That is reachable wherever a route forwards a user-supplied identifier into a path: a validator that permits `.` and `/`,
plus a normalizer that only uppercases, leaves nothing between the route and the client to remove the traversal.

The same lack of escaping is an ordinary correctness bug wherever a legitimate
identifier contains a slash: a crypto symbol like ``BASE/QUOTE``, so
``BTC/USD`` builds ``/v2/positions/BTC/USD`` — a different endpoint that
simply 404s instead of closing the position.

Escaping belongs in the CLIENT because the client is what owns URL
construction; every layer above it legitimately deals in unescaped values.
"""

from __future__ import annotations

import httpx2
import pytest

from headgate._url import path, seg


class TestSegHelper:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("AAPL", "AAPL"),
            ("BRK.B", "BRK.B"),  # a dot is legal in a segment and must survive
            ("BTC/USD", "BTC%2FUSD"),
            ("../../v2/account", "..%2F..%2Fv2%2Faccount"),
            ("a b", "a%20b"),
            ("a?b#c", "a%3Fb%23c"),
            (".", "%2E"),  # an all-dots value is a STRUCTURAL dot segment, not data
            ("..", "%2E%2E"),
        ],
    )
    def test_escapes_structural_characters(self, raw: str, expected: str) -> None:
        assert seg(raw) == expected

    def test_accepts_non_str(self) -> None:
        """Ids arrive as UUID/int at some call sites."""
        from uuid import UUID

        assert seg(7) == "7"
        assert seg(UUID("12345678-1234-5678-1234-567812345678")) == "12345678-1234-5678-1234-567812345678"


class TestBuiltUrls:
    """End-to-end through httpx2: the escaped segment cannot restructure the URL."""

    @staticmethod
    def _url(path: str) -> str:
        client = httpx2.Client(base_url="https://api.example.test/")
        return str(client.build_request("DELETE", path).url)

    def test_traversal_stays_one_segment(self) -> None:
        assert self._url(f"/v2/positions/{seg('../../v2/account')}") == (
            "https://api.example.test/v2/positions/..%2F..%2Fv2%2Faccount"
        )

    def test_unescaped_traversal_would_escape_the_endpoint(self) -> None:
        """The negative control — proves the escaping above is load-bearing.

        Without ``seg`` this same input silently retargets the request, which
        is exactly the bug being fixed.
        """
        assert self._url("/v2/positions/../../v2/account") == "https://api.example.test/v2/account"

    def test_slash_bearing_symbol_stays_one_segment(self) -> None:
        assert self._url(f"/v2/positions/{seg('BTC/USD')}") == ("https://api.example.test/v2/positions/BTC%2FUSD")

    def test_dot_segment_cannot_retarget(self) -> None:
        assert self._url(f"/v2/positions/{seg('.')}") == "https://api.example.test/v2/positions/%2E"

    def test_unescaped_dot_would_hit_close_all_positions(self) -> None:
        """Negative control: proves the escaping above is load-bearing.

        ``DELETE /v2/positions`` is Example's liquidate-everything endpoint.
        """
        assert self._url("/v2/positions/.") == "https://api.example.test/v2/positions"


class TestPathRenderer:
    """``path()`` escapes by DEFAULT; only a nested Template is trusted."""

    def test_literal_only(self) -> None:
        assert path(t"/api/news/feed") == "/api/news/feed"

    def test_interpolation_is_escaped_by_default(self) -> None:
        symbol = "BTC/USD"
        assert path(t"/v2/positions/{symbol}") == "/v2/positions/BTC%2FUSD"

    def test_traversal_is_neutralised(self) -> None:
        evil = "../../v2/account"
        assert path(t"/v2/positions/{evil}") == "/v2/positions/..%2F..%2Fv2%2Faccount"

    def test_nested_template_splices_verbatim(self) -> None:
        prefix = t"/api/v3/brokerage"
        assert path(t"{prefix}/accounts") == "/api/v3/brokerage/accounts"

    def test_nested_template_is_recursive(self) -> None:
        inner = t"/api"
        mid = t"{inner}/v3"
        assert path(t"{mid}/accounts") == "/api/v3/accounts"

    def test_nested_template_escapes_its_own_holes(self) -> None:
        """A trusted prefix does NOT launder the values inside it."""
        sym = "A/B"
        inner = t"/x/{sym}"
        assert path(t"{inner}/y") == "/x/A%2FB/y"

    def test_raw_spec_is_the_escape_hatch(self) -> None:
        frag = "a/b"
        assert path(t"/x/{frag:raw}") == "/x/a/b"

    def test_format_spec_is_applied_then_escaped(self) -> None:
        year, month = 2026, 3
        assert path(t"cnsfails{year:04d}{month:02d}.zip") == "cnsfails202603.zip"

    def test_date_format_spec(self) -> None:
        from datetime import date

        d = date(2026, 1, 5)
        assert path(t"/shvol{d:%Y%m%d}.txt") == "/shvol20260105.txt"

    def test_non_str_values(self) -> None:
        from uuid import UUID

        assert path(t"/o/{7}") == "/o/7"
        u = UUID("12345678-1234-5678-1234-567812345678")
        assert path(t"/o/{u}") == "/o/12345678-1234-5678-1234-567812345678"

    def test_dot_survives(self) -> None:
        """``BRK.B`` is a real ticker; a dot is legal inside a segment."""
        s = "BRK.B"
        assert path(t"/q/{s}") == "/q/BRK.B"

    def test_bare_dot_is_escaped(self) -> None:
        s = "."
        assert path(t"/v2/positions/{s}") == "/v2/positions/%2E"

    def test_format_spec_output_is_still_escaped(self) -> None:
        """The spec is applied, and its OUTPUT still goes through ``seg``.

        ``test_format_spec_is_applied_then_escaped`` only formats digits, so a
        variant that applied the spec and skipped the escape would pass it.
        """
        v = "a/b"
        assert path(t"/x/{v:>4}") == "/x/%20a%2Fb"

    def test_raw_is_ignored_on_a_template_value(self) -> None:
        """The ``Template`` branch precedes the ``raw`` branch — deliberately.

        The alternative reading would let ``:raw`` on a wrapper launder every
        value nested inside it.
        """
        sym = "A/B"
        inner = t"/x/{sym}"
        assert path(t"{inner:raw}/y") == path(t"{inner}/y") == "/x/A%2FB/y"


class TestPathRejectsAmbiguousInput:
    """A security primitive fails loudly rather than guessing."""

    def test_conversion_raises(self) -> None:
        v = "a/b"
        with pytest.raises(ValueError, match=r"conversion !r is not supported"):
            path(t"/x/{v!r}")

    @pytest.mark.parametrize("conversion", ["r", "s", "a"])
    def test_every_conversion_raises(self, conversion: str) -> None:
        v = "a/b"
        templates = {"r": t"/x/{v!r}", "s": t"/x/{v!s}", "a": t"/x/{v!a}"}
        with pytest.raises(ValueError, match=rf"conversion !{conversion} is not supported"):
            path(templates[conversion])

    def test_conversion_inside_a_nested_template_raises(self) -> None:
        """A prefix is trusted for what it IS, not licensed to skip the checks."""
        v = "a/b"
        inner = t"/x/{v!r}"
        with pytest.raises(ValueError, match=r"conversion !r is not supported"):
            path(t"{inner}/y")

    def test_f_string_raises(self) -> None:
        """Iterating a ``str`` yields characters that all take the literal branch.

        Without this guard ``path()`` silently returns the f-string's already
        interpolated, UNESCAPED text.
        """
        sym = "BTC/USD"
        with pytest.raises(TypeError, match=r"takes a t-string Template, got str"):
            path(f"/v2/positions/{sym}")  # ty: ignore[invalid-argument-type]  # the TypeError IS the test

    def test_plain_str_raises(self) -> None:
        with pytest.raises(TypeError, match=r"takes a t-string Template, got str"):
            path("/v2/positions")  # ty: ignore[invalid-argument-type]  # the TypeError IS the test


class TestPathBuiltUrls:
    """``path()`` output through httpx2 — dot resolution is only visible here."""

    @staticmethod
    def _url(rendered: str) -> str:
        client = httpx2.Client(base_url="https://api.example.test/")
        return str(client.build_request("DELETE", rendered).url)

    def test_traversal_stays_one_segment(self) -> None:
        evil = "../../v2/account"
        assert self._url(path(t"/v2/positions/{evil}")) == (
            "https://api.example.test/v2/positions/..%2F..%2Fv2%2Faccount"
        )

    def test_slash_bearing_symbol_stays_one_segment(self) -> None:
        sym = "BTC/USD"
        assert self._url(path(t"/v2/positions/{sym}")) == ("https://api.example.test/v2/positions/BTC%2FUSD")

    def test_dot_segment_cannot_retarget(self) -> None:
        """Without the ``seg`` dot fix this collapses to the liquidate-all endpoint."""
        sym = "."
        assert self._url(path(t"/v2/positions/{sym}")) == "https://api.example.test/v2/positions/%2E"

    def test_double_dot_segment_cannot_climb(self) -> None:
        sym = ".."
        assert self._url(path(t"/v2/positions/{sym}")) == "https://api.example.test/v2/positions/%2E%2E"
