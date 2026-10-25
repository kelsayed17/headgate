# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``route_template``: the endpoint a t-string URL names, with the values left out."""

from __future__ import annotations

from datetime import date

from headgate._url import route_template


def test_each_hole_is_written_as_its_expression() -> None:
    """Write each interpolation as ``{expression}``, whatever its value, so calls group by endpoint."""
    series_id, vintage = "GDP/real", date(2026, 10, 24)
    assert route_template(t"series/{series_id}/vintages/{vintage:%Y%m%d}") == "series/{series_id}/vintages/{vintage}"


def test_a_nested_template_contributes_its_own_route() -> None:
    """Splice a nested template's route, holes and all, and write a raw fragment as its expression."""
    version = "v3"
    prefix = t"/api/{version}"
    fragment = "a/b"
    assert route_template(t"{prefix}/files/{fragment:raw}") == "/api/{version}/files/{fragment}"
