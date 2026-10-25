# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""An empty URL path segment is refused.

``t"/v2/positions/{symbol}"`` with ``symbol=""`` would render ``DELETE /v2/positions/``: one trailing slash from a
collection endpoint (close EVERY position), and a router that strips the slash with a 301/308 re-sends the DELETE
there. ``escape_segment`` refuses the empty value just as it neutralises the ``.`` / ``..`` dot segments.
"""

from __future__ import annotations

import pytest

from headgate._url import escape_segment, render_path


def test_an_empty_segment_is_refused() -> None:
    with pytest.raises(ValueError, match="empty URL path segment"):
        escape_segment("")


def test_an_empty_interpolation_is_refused_before_a_url_exists() -> None:
    symbol = ""
    with pytest.raises(ValueError, match="empty URL path segment"):
        render_path(t"/v2/positions/{symbol}")


def test_a_real_segment_still_renders() -> None:
    symbol = "BRK.B"
    assert render_path(t"/v2/positions/{symbol}") == "/v2/positions/BRK.B"
