"""A capped response reports the URL that ANSWERED, not the one we asked for.

``AsyncBaseClient`` re-builds every response after capping its body (``read_capped``), and httpx2 derives
``Response.url`` from the request the response carries. Rebuilding with the ORIGINAL request made every capped
response claim the URL we SENT — indistinguishable, to a caller, from one that was never redirected.

That is not cosmetic: a client can detect a missing or invalid key by a **302 to an HTML landing page under HTTP 200**
rather than a 401. With the original request attached, the landing page's URL never reached that branch, so it could
not fire, and a bad key surfaced as a generic ``ResponseParseError``, indistinguishable from real no-data. (A client now
refuses the redirect and reads the 302's ``Location`` through the base client's ``refusal`` check instead; the property
pinned here is the base client's and stands on its own.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate._client import AsyncBaseClient
from headgate._rate_limit import Rate
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx2 import HTTPXMock


class _Client(AsyncBaseClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://example.test", rate=Rate.per_second(1000.0))


def _redirecting(httpx2_mock: HTTPXMock) -> None:
    """``/start`` answers a 302 to ``/final``, which answers 200 — each hop registered once, so both must be sent."""
    httpx2_mock.add_response(
        status_code=302, headers={"Location": "https://example.test/final"}, url="https://example.test/start"
    )
    httpx2_mock.add_response(text="landed", url="https://example.test/final")


async def test_capped_response_reports_the_final_url(httpx2_mock: HTTPXMock) -> None:
    client = _Client()
    _redirecting(httpx2_mock)
    resp = await client.get("start", expect=RESPONSE)
    assert resp.url.path == "/final", resp.url
    await client.aclose()


async def test_capped_response_keeps_the_redirect_history(httpx2_mock: HTTPXMock) -> None:
    """A rebuild that drops the chain is not the response it claims to be."""
    client = _Client()
    _redirecting(httpx2_mock)
    resp = await client.get("start", expect=RESPONSE)
    assert [r.status_code for r in resp.history] == [302]
    assert resp.history[0].url.path == "/start"
    await client.aclose()


async def test_no_redirect_still_reports_its_own_url(httpx2_mock: HTTPXMock) -> None:
    """The ordinary path is unchanged — this is not a redirect-only behaviour."""
    client = _Client()
    httpx2_mock.add_response(text="ok")
    resp = await client.get("plain", expect=RESPONSE)
    assert resp.url.path == "/plain"
    assert resp.history == []
    await client.aclose()
