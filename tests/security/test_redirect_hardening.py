# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Redirects never downgrade ``https`` to ``http``, and ``max_redirects`` bounds every chain.

A downgrade is not followed: the 3xx becomes the call's final response and raises through the status ladder as the
``UpstreamStatusError`` it is, with a note naming the refused target (credentials redacted).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import CachedTokenAuth, Expect, HttpOptions, Minted, TransportError, UpstreamStatusError
from tests.security._servers import client_ssl_context, serving
from tests.security._web import BASE, Web
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from tests.security._servers import Answer, Seen

from headgate.ratelimit import Rate


async def test_a_downgrade_is_raised_as_the_redirect() -> None:
    """Refuse a redirect from ``https`` to ``http``: one request sent, the 302 raised with a note naming the target."""
    web = Web({f"{BASE}/start": "http://api.example/x?api_key=hunter2"})
    async with web.client() as client:
        with pytest.raises(UpstreamStatusError) as exc:
            await client.get("start", expect=RESPONSE)

    assert exc.value.status_code == httpx2.codes.FOUND
    assert len(web.sent) == 1
    notes = "\n".join(exc.value.__notes__)
    assert "refused to follow a redirect from https to http://api.example/x" in notes
    assert "hunter2" not in notes


async def test_a_redirect_the_call_does_not_follow_keeps_its_location() -> None:
    """Leave a downgrade the call does not follow whole, so a declared 3xx still reads its ``Location``."""
    web = Web({f"{BASE}/start": "http://api.example/x"})

    def location(resp: httpx2.Response) -> str:
        """Return the redirect's target."""
        return resp.headers["Location"]

    async with web.client() as client:
        target = await client.get(
            "start", expect=Expect(RESPONSE).on(httpx2.codes.FOUND, location), follow_redirects=False
        )

    assert target == "http://api.example/x"


@pytest.mark.parametrize(
    ("base_url", "target"),
    [
        (BASE, "/relative"),
        (BASE, "//files.example/scheme-relative"),
        (BASE, "https://files.example/secure"),
        ("http://api.example", "http://files.example/plain"),
    ],
    ids=["relative", "scheme-relative", "https", "http-to-http"],
)
async def test_a_redirect_that_does_not_downgrade_is_followed(base_url: str, target: str) -> None:
    """Follow every redirect that keeps the scheme (or upgrades it)."""
    web = Web({f"{base_url}/start": target})
    async with web.client(base_url=base_url) as client:
        resp = await client.get("start", expect=RESPONSE)

    assert resp.text == "ok"
    assert len(web.sent) == len(("start", "target"))


async def test_an_unparsable_location_is_left_for_httpx2_to_refuse() -> None:
    """Leave a ``Location`` httpx2 cannot parse to httpx2, which refuses it as a protocol error."""
    web = Web({f"{BASE}/start": "http://a:xx/"})
    async with web.client() as client:
        with pytest.raises(TransportError, match="location header"):
            await client.get("start", expect=RESPONSE)


class _DowngradedMint(CachedTokenAuth):
    """Mint over a hop whose token endpoint redirects to ``http``, recording what the hop got back."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        super().__init__()
        self.hop_answer: httpx2.Response | None = None

    @override
    async def _mint_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        self.hop_answer = yield httpx2.Request("POST", "https://login.example/token")
        yield Minted("token", 3600.0)


async def test_an_auth_hop_is_not_downgraded_either() -> None:
    """Hand a mint hop's downgrade back to the mint as the 302 it is, without following it."""
    web = Web({"https://login.example/token": "http://login.example/token"})
    auth = _DowngradedMint()
    async with web.client(auth=auth) as client:
        await client.get("data", expect=RESPONSE)

    assert auth.hop_answer is not None
    assert auth.hop_answer.status_code == httpx2.codes.FOUND
    assert [request.url.scheme for request in web.sent] == ["https", "https"]


#: The redirects in the chain :func:`test_max_redirects_bounds_the_chain` builds.
_CHAIN = 3


@pytest.mark.parametrize(("max_redirects", "follows"), [(_CHAIN, True), (_CHAIN - 1, False)], ids=["enough", "short"])
async def test_max_redirects_bounds_the_chain(max_redirects: int, *, follows: bool) -> None:
    """Follow a chain of redirects up to ``max_redirects`` and raise past it, sending no further hop."""
    web = Web({f"{BASE}/{hop}": f"{BASE}/{hop + 1}" for hop in range(_CHAIN)})
    async with web.client(http=HttpOptions(max_redirects=max_redirects)) as client:
        if follows:
            await client.get("0", expect=RESPONSE)
        else:
            with pytest.raises(TransportError, match="redirects"):
                await client.get("0", expect=RESPONSE)

    assert len(web.sent) == (_CHAIN + 1 if follows else _CHAIN)


async def test_a_real_tls_server_cannot_downgrade_to_a_plain_one() -> None:
    """Refuse a real TLS server's redirect to a real plain-HTTP server, which receives nothing."""
    async with serving() as plain:

        def redirect(_seen: Seen) -> Answer:
            """Redirect every request to the plain server."""
            return httpx2.codes.FOUND, {"Location": plain.url("/leak")}, b""

        async with serving(redirect, tls=True) as secure:
            client = StubClient(
                base_url=secure.url(host="localhost"),
                rate=Rate.per_second(1000),
                http=HttpOptions(ssl_context=client_ssl_context()),
            )
            async with client:
                with pytest.raises(UpstreamStatusError) as exc:
                    await client.get("start", expect=RESPONSE)

    assert exc.value.status_code == httpx2.codes.FOUND
    assert len(secure.requests) == 1
    assert plain.connections == 0
