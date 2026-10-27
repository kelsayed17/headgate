# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``Expect(..., accept=)``: the call sends ``Accept`` and refuses a body of another media type before decoding it."""

from __future__ import annotations

import json

import httpx2
import pytest

from headgate import Expect, HttpOptions, as_bytes, json_reader, nothing
from headgate._responses import accepts
from headgate.errors import ResponseParseError, UpstreamStatusError
from headgate.testing import FakeTransport
from tests.observe._support import client

_JSON = "application/json"
_BODY = b'{"ok": true}'


def _answering(content_type: str | None, *, status: int = 200, content: bytes = _BODY) -> FakeTransport:
    """Return a fake answering ``GET /v1/x`` with *content* declared as *content_type*."""
    headers = {} if content_type is None else {"content-type": content_type}
    return FakeTransport().reply("GET", "/v1/x", httpx2.Response(status, content=content, headers=headers))


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/json; charset=utf-8",
        "Application/JSON",
        "application/problem+json",
        "application/vnd.api+v2+json",
    ],
)
async def test_a_matching_media_type_is_decoded(content_type: str) -> None:
    """Read a body whose type matches, its parameters ignored and a ``+json`` suffix accepted."""
    fake = _answering(content_type)
    body = await client(fake).get("x", expect=Expect(as_bytes, accept=_JSON))

    assert body == _BODY
    assert fake.calls[0].headers["accept"] == _JSON


@pytest.mark.parametrize("content_type", ["text/html", None, "not a media type", "text/json", "application/xml"])
async def test_another_media_type_raises_before_the_reader_runs(content_type: str | None) -> None:
    """Raise ``ResponseParseError`` with the status and the declared type, never calling the reader."""

    def must_not_run(resp: httpx2.Response) -> bytes:
        raise AssertionError(resp)

    with pytest.raises(ResponseParseError) as caught:
        await client(_answering(content_type)).get("x", expect=Expect(must_not_run, accept=_JSON))

    error = caught.value
    assert (error.status_code, error.content_type) == (httpx2.codes.OK, content_type)
    assert f"expected {_JSON}, got {content_type or 'no Content-Type'}" in str(error)


async def test_an_empty_body_has_nothing_to_check() -> None:
    """Pass a body-less answer (a 204) whatever its headers say."""
    fake = _answering(None, status=httpx2.codes.NO_CONTENT, content=b"")
    await client(fake).get("x", expect=Expect(nothing, accept=_JSON))


async def test_a_declared_non_2xx_of_another_type_falls_through_to_the_status_ladder() -> None:
    """Treat a declared 404 whose body is not the accepted type like one its reader cannot read."""
    fake = _answering("text/html", status=httpx2.codes.NOT_FOUND)
    expect = Expect(as_bytes, accept=_JSON).on(httpx2.codes.NOT_FOUND, as_bytes)
    with pytest.raises(UpstreamStatusError) as caught:
        await client(fake).get("x", expect=expect)
    assert caught.value.status_code == httpx2.codes.NOT_FOUND


@pytest.mark.parametrize(
    ("accept", "content_type"),
    [("text/*", "text/csv"), ("*/*", "image/png"), ("text/csv, application/json", "application/json")],
)
async def test_ranges_and_lists_match_as_accept_does(accept: str, content_type: str) -> None:
    """Match ``type/*``, ``*/*`` and any member of a list."""
    assert await client(_answering(content_type)).get("x", expect=Expect(as_bytes, accept=accept)) == _BODY


async def test_the_calls_own_accept_header_wins() -> None:
    """Keep an ``Accept`` the call set, while still checking the response against ``accept``."""
    fake = _answering(_JSON)
    await client(fake).get("x", expect=Expect(as_bytes, accept=_JSON), headers={"ACCEPT": "application/*"})
    assert fake.calls[0].headers["accept"] == "application/*"


async def test_accept_replaces_the_clients_default_header() -> None:
    """Send ``accept`` over an ``Accept`` among the client's default headers."""
    fake = _answering(_JSON)
    http = HttpOptions(headers={"Accept": "text/plain"})
    await client(fake, http=http).get("x", expect=Expect(as_bytes, accept=_JSON), headers={"X-Other": "1"})
    assert fake.calls[0].headers.get_list("accept") == [_JSON]


async def test_a_view_header_counts_as_the_calls_own() -> None:
    """Keep an ``Accept`` a ``with_options`` view set."""
    fake = _answering(_JSON)
    view = client(fake).with_options(headers={"Accept": "application/*"})
    await view.get("x", expect=Expect(as_bytes, accept=_JSON))
    assert fake.calls[0].headers["accept"] == "application/*"


def test_on_keeps_accept() -> None:
    """Carry ``accept`` to the ``Expect`` that :meth:`Expect.on` returns."""
    assert Expect(as_bytes, accept=_JSON).on(httpx2.codes.NOT_FOUND, nothing).accept == _JSON


@pytest.mark.parametrize("accept", ["", "json", "application/json,", "application/ json"])
def test_a_malformed_accept_is_refused_where_it_is_written(accept: str) -> None:
    """Raise ``ValueError`` for anything that is not a list of media types."""
    with pytest.raises(ValueError, match="accept must name media types"):
        Expect(as_bytes, accept=accept)


def test_without_accept_every_response_is_accepted() -> None:
    """Accept any response when ``accept`` is not given."""
    assert accepts(Expect(as_bytes), httpx2.Response(200, content=b"<html>", headers={"content-type": "text/html"}))


def test_json_reader_refuses_an_empty_body_unless_allowed() -> None:
    """Refuse an empty body by default: an empty 200 is corruption for every provider but a list-shaped one."""
    with pytest.raises(json.JSONDecodeError):
        json_reader(json.loads)(httpx2.Response(200, content=b""))
