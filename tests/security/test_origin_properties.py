# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The origin rule holds for any redirect chain: a credential never reaches an untrusted origin.

Hypothesis draws an auth, a first URL (relative, or absolute on any origin) and a chain of redirects across trusted and
untrusted origins, some echoing the query credential into ``Location``. Every request that reaches the wire is checked:
one addressed to an untrusted origin carries none of the auth's declared credentials, nor ``Authorization``,
``Proxy-Authorization`` or ``Cookie``; and a first request to the client's own origin carries the auth's credential.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final, override

from hypothesis import given, settings
from hypothesis import strategies as st

from headgate import CachedTokenAuth, HttpOptions, QueryParamAuth, StaticHeaderAuth, UpstreamStatusError
from headgate._core import origin_of
from tests.security._web import BASE, Web
from tests.support.responses import RESPONSE
from tests.support.tokens import premint

if TYPE_CHECKING:
    import httpx2

#: The origins a hop may be addressed to.
_ORIGINS: Final = (
    BASE,
    "https://files.example",
    "https://cdn.example",
    "https://api.example:8443",
    "http://api.example",
    "https://evil.example",
)
#: The origin trusted besides the client's own.
_TRUSTED: Final = "https://files.example"
#: Headers withheld from an untrusted origin whatever the auth.
_ALWAYS: Final = ("authorization", "proxy-authorization", "cookie")
#: Enough hops for any drawn chain to be followed to its end.
_MAX_REDIRECTS: Final = 10


class _Bearer(CachedTokenAuth):
    """A bearer auth whose exchange never runs: it is seeded."""

    @override
    async def mint_token(self) -> tuple[str, float]:
        raise NotImplementedError


def _auth(kind: str) -> StaticHeaderAuth | QueryParamAuth | CachedTokenAuth:
    """Return a fresh auth of *kind*, ready to stamp without a network exchange."""
    if kind == "header":
        return StaticHeaderAuth({"X-Api-Key": "k"})
    if kind == "query":
        return QueryParamAuth({"api_key": "k"})
    return premint(_Bearer())


_HOP = st.tuples(st.sampled_from(_ORIGINS), st.booleans())


@settings(deadline=None)
@given(
    kind=st.sampled_from(["header", "query", "bearer"]),
    first=st.one_of(st.just(""), st.sampled_from(_ORIGINS)),
    chain=st.lists(_HOP, max_size=5),
)
def test_no_credential_reaches_an_untrusted_origin(kind: str, first: str, chain: list[tuple[str, bool]]) -> None:
    """Withhold every credential from every untrusted hop, across any chain of redirects and absolute URLs."""
    auth = _auth(kind)
    start = f"{first or BASE}/h0"
    redirects = {}
    previous = start
    for index, (origin, echo) in enumerate(chain, start=1):
        target = f"{origin}/h{index}"
        redirects[previous] = target + ("?api_key=k" if echo else "")
        previous = target
    web = Web(redirects)
    options = HttpOptions(trusted_origins=frozenset({_TRUSTED}), max_redirects=_MAX_REDIRECTS)

    async def run() -> None:
        async with web.client(auth=auth, http=options) as client:
            # A drawn downgrade from https to http ends the chain as a raised 3xx; the hops before it still count.
            with contextlib.suppress(UpstreamStatusError):
                await client.get(
                    start if first else "h0", expect=RESPONSE, headers={"Cookie": "c", "Proxy-Authorization": "p"}
                )

    asyncio.run(run())

    trusted = {origin_of(BASE), origin_of(_TRUSTED)}
    withheld = {*auth.credential_headers, *_ALWAYS}
    for request in web.sent:
        if request.url.origin in trusted:
            continue
        assert not withheld & {name.lower() for name in request.headers}, request
        assert not auth.credential_params & set(request.url.params), request
    if not first or first == BASE:
        sent = web.sent[0]
        assert {*auth.credential_headers} & {name.lower() for name in sent.headers} or auth.credential_params & set(
            sent.url.params
        )


@given(origin=st.sampled_from(_ORIGINS), path=st.text(alphabet="abcxyz/", max_size=8))
def test_the_guard_decision_depends_only_on_the_origin(origin: str, path: str) -> None:
    """Strip on exactly the untrusted origins, whatever the path."""
    web = Web()

    async def run() -> httpx2.Request:
        async with web.client(auth=StaticHeaderAuth({"X-Api-Key": "k"})) as client:
            await client.get(f"{origin}/{path}", expect=RESPONSE)
        return web.sent[0]

    sent = asyncio.run(run())

    assert ("X-Api-Key" in sent.headers) == (origin_of(origin) == origin_of(BASE))
