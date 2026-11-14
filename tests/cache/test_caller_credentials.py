# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A call that sets the auth's own credential itself is partitioned by the value it sent, not by the auth's identity.

Every auth headgate ships leaves a header or parameter the call set alone, so the request carries the call's credential
under a name the cache would otherwise represent by the auth's identity. Without the call's value in the label, a
response cached for one caller's ``Authorization`` would be served to the next caller's, ``Vary`` or not: the view
drops the header that ``Vary`` names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import (
    BearerAuth,
    CachedTokenAuth,
    Client,
    HttpOptions,
    Minted,
    QueryParamAuth,
    StaticHeaderAuth,
    TransportPolicy,
)
from headgate.cache import HttpCache, Partition
from headgate.cache._credentials import CALLER_CREDENTIALS, CallerCredentials, CredentialScope, mark_caller_credentials
from headgate.cache._transport import served_from_cache
from tests.cache._helpers import series_rules
from tests.support.responses import RESPONSE
from tests.support.tokens import premint

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from hishel import AsyncSqliteStorage

_URL = "https://x/series?id=A"


class _Bearer(CachedTokenAuth):
    """A bearer auth whose exchange never runs: tests seed it."""

    @override
    async def mint_token(self) -> tuple[str, float]:
        raise NotImplementedError


class _Echo:
    """The network leg: answers with the credential each request carried, varying on ``Authorization``."""

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.sent: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record and echo the request's ``Authorization``, or its ``api_key``."""
        self.sent.append(request.headers.get("Authorization") or request.url.params["api_key"])
        return httpx2.Response(httpx2.codes.OK, headers={"Vary": "Authorization"}, text=self.sent[-1])


def _client(echo: _Echo, storage: AsyncSqliteStorage, auth: httpx2.Auth) -> Client:
    """Build a client caching ``/series`` into *storage* with *auth*, over *echo*."""
    return Client(
        base_url="https://x",
        auth=auth,
        policy=TransportPolicy(cache=HttpCache(**series_rules(), storage=storage)),
        http=HttpOptions(transport=httpx2.MockTransport(echo)),
    )


def _own(name: str | None, key: str) -> dict[str, str]:
    """Return *key* under *name*, or nothing when there is no *name*: a call's ``headers=`` or ``params=``."""
    return {} if name is None else {name: key}


@pytest.mark.parametrize(
    ("auth", "header", "param", "stamped"),
    [
        (BearerAuth("service"), "Authorization", None, "Bearer service"),
        (premint(_Bearer()), "Authorization", None, "Bearer test-token"),
        (QueryParamAuth({"api_key": "service"}), None, "api_key", "service"),
    ],
    ids=["bearer", "cached-token", "query-param"],
)
async def test_a_calls_own_credential_gets_entries_of_its_own(
    auth: httpx2.Auth, header: str | None, param: str | None, stamped: str, storage: AsyncSqliteStorage
) -> None:
    """Keep the entries of a call's own ``Authorization`` or ``api_key`` apart per value, and from the auth's."""
    echo = _Echo()
    async with _client(echo, storage, auth) as client:
        alice = await client.get(_URL, expect=RESPONSE, headers=_own(header, "alice"), params=_own(param, "alice"))
        bob = await client.get(_URL, expect=RESPONSE, headers=_own(header, "bob"), params=_own(param, "bob"))
        service = await client.get(_URL, expect=RESPONSE)
        hit = await client.get(_URL, expect=RESPONSE, headers=_own(header, "alice"), params=_own(param, "alice"))

    assert [alice.text, bob.text, service.text] == ["alice", "bob", stamped]
    assert echo.sent == ["alice", "bob", stamped]
    assert hit.text == "alice"
    assert served_from_cache(hit)


def _label(scope: CredentialScope, request: httpx2.Request) -> str | None:
    """Return the partition label *scope* gives *request*."""
    stored, _ = scope.view(request)
    return stored.fragment or None


def _marked(headers: dict[str, str], *, caller: CallerCredentials | None = None, url: str = _URL) -> httpx2.Request:
    """Return a GET of *url* with *headers*, marked as setting *caller*'s names itself when given."""
    extensions = {} if caller is None else {CALLER_CREDENTIALS: caller}
    return httpx2.Request("GET", url, headers=headers, extensions=extensions)


def test_a_marked_credential_labels_by_its_value_and_an_unmarked_one_by_the_identity() -> None:
    """Label by the value only a name the call set itself; the auth's own stamp stays represented by its identity."""
    scope = CredentialScope(StaticHeaderAuth({"X-Api-Key": "service"}))
    caller = CallerCredentials(headers=frozenset({"x-api-key"}), params=frozenset())

    alice, bob = (_label(scope, _marked({"X-Api-Key": key}, caller=caller)) for key in ("alice", "bob"))
    stamped, rotated = (_label(scope, _marked({"X-Api-Key": key})) for key in ("service", "rotated"))

    assert alice != bob
    assert stamped == rotated
    assert stamped not in {alice, bob}


def test_a_marked_parameter_labels_by_its_value() -> None:
    """Label by the value of a parameter the call set itself, under the shared partition too."""
    for partition in Partition:
        scope = CredentialScope(QueryParamAuth({"api_key": "service"}), partition=partition)
        caller = CallerCredentials(headers=frozenset(), params=frozenset({"api_key"}))

        alice = _label(scope, _marked({}, caller=caller, url=f"{_URL}&api_key=alice"))
        bob = _label(scope, _marked({}, caller=caller, url=f"{_URL}&api_key=bob"))

        assert None not in (alice, bob)
        assert alice != bob


async def _flow(auth: httpx2.Auth, request: httpx2.Request) -> list[httpx2.Request]:
    """Drive *auth*'s flow for *request*, answering every request it yields with a 200, and return them."""
    flow = auth.async_auth_flow(request)
    sent = [await anext(flow)]
    try:
        while True:
            sent.append(await flow.asend(httpx2.Response(httpx2.codes.OK, text="token", request=sent[-1])))
    except StopAsyncIteration:
        return sent
    finally:
        await flow.aclose()


async def test_the_mark_names_what_the_call_set_before_the_auth_ran() -> None:
    """Mark the auth's declared names the request already carried, and leave the auth's flow as it was."""
    auth = mark_caller_credentials(StaticHeaderAuth({"X-Api-Key": "service", "X-Other": "o"}))
    assert auth is not None
    request = httpx2.Request("GET", f"{_URL}&api_key=a", headers={"x-api-key": "alice"})

    (sent,) = await _flow(auth, request)

    assert sent.extensions[CALLER_CREDENTIALS] == CallerCredentials(
        headers=frozenset({"x-api-key"}), params=frozenset()
    )
    assert (sent.headers["X-Api-Key"], sent.headers["X-Other"]) == ("alice", "o")


class _Minting(CachedTokenAuth):
    """Mint with one hop to a token host, so its flow yields two requests."""

    @override
    async def mint_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        response = yield httpx2.Request("POST", "https://login.example/token")
        yield Minted(response.text, 3600.0)


async def test_the_mark_passes_every_hop_of_the_auths_flow_through() -> None:
    """Hand each request the auth's flow yields to the client and each response back, to the end of the flow."""
    auth = mark_caller_credentials(_Minting())
    assert auth is not None

    hop, call = await _flow(auth, httpx2.Request("GET", _URL))

    assert str(hop.url) == "https://login.example/token"
    assert call.headers["Authorization"] == "Bearer token"
    assert call.extensions[CALLER_CREDENTIALS] == CallerCredentials(headers=frozenset(), params=frozenset())


def test_only_a_credential_stamper_is_marked() -> None:
    """Leave no auth, and an auth that declares nothing, as they are: neither has a name to mark."""
    basic = httpx2.BasicAuth("user", "pass")

    assert mark_caller_credentials(None) is None
    assert mark_caller_credentials(basic) is basic
