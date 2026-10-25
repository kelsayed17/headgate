# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The cache and the cassette filter read what the real auths declare about their credentials.

The cache partitions entries by each auth's ``credential_identity()``, so two credentials on one storage never share
an entry and one credential always finds its own; the cassette filter withholds the headers and parameters they stamp.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, override

import httpx2
import pytest

from headgate import HttpCache, QueryParamAuth, StaticHeaderAuth, TransportPolicy, as_bytes
from headgate.testing import CassetteFilter, FakeTransport
from tests.cache._helpers import every_get_policy
from tests.integration._support import Bearer, client, seeded

if TYPE_CHECKING:
    from collections.abc import Callable

    from hishel import AsyncSqliteStorage

#: Builds an auth holding the credential it is given.
type _Holding = Callable[[str], httpx2.Auth]


class _KeyedBearer(Bearer):
    """A minted bearer whose long-lived credential is an API key, which it names as its identity."""

    def __init__(self, api_key: str) -> None:
        """Mint with *api_key*."""
        super().__init__()
        self._api_key = api_key

    @override
    def credential_identity(self) -> bytes:
        return self._api_key.encode()


def _keyed_bearer(api_key: str) -> httpx2.Auth:
    """Return a :class:`_KeyedBearer` for *api_key* holding a token of its own, as a fresh mint would."""
    auth = _KeyedBearer(api_key)
    auth.seed(f"token-{id(auth)}", math.inf)
    return auth


_HOLDERS: dict[str, _Holding] = {
    "static-header": lambda secret: StaticHeaderAuth({"X-Api-Key": secret}),
    "query-param": lambda secret: QueryParamAuth({"api_key": secret}),
    "cached-token-keyed": _keyed_bearer,
}


def _cached(storage: AsyncSqliteStorage) -> TransportPolicy:
    """Return a policy caching every GET into *storage*."""
    return TransportPolicy(cache=HttpCache(policy=every_get_policy(), storage=storage))


@pytest.mark.parametrize("name", sorted(_HOLDERS))
async def test_two_credentials_never_share_an_entry(name: str, storage: AsyncSqliteStorage) -> None:
    """Make each credential fetch its own copy of a URL the other has cached, and then hit only its own."""
    fake = FakeTransport().route("GET", "/v1/x", httpx2.Response(200, content=b"private"))
    alice, bob = (_HOLDERS[name](secret) for secret in ("alice-secret", "bob-secret"))
    alice_client = client(fake, auth=alice, policy=_cached(storage))
    bob_client = client(fake, auth=bob, policy=_cached(storage))

    for built in (alice_client, bob_client, alice_client, bob_client):
        await built.get("x", expect=as_bytes)

    assert len(fake.calls) == len(("alice", "bob"))


@pytest.mark.parametrize("name", sorted(_HOLDERS))
async def test_one_credential_shares_its_entries_between_clients(name: str, storage: AsyncSqliteStorage) -> None:
    """Serve a second client holding the same credential from the first one's entry, even with its own token."""
    fake = FakeTransport().route("GET", "/v1/x", httpx2.Response(200, content=b"private"))
    first, second = (_HOLDERS[name]("shared-secret") for _ in range(2))

    await client(fake, auth=first, policy=_cached(storage)).get("x", expect=as_bytes)
    await client(fake, auth=second, policy=_cached(storage)).get("x", expect=as_bytes)

    assert len(fake.calls) == 1


async def test_two_cached_token_auths_share_nothing_by_default(storage: AsyncSqliteStorage) -> None:
    """Keep two ``CachedTokenAuth`` instances apart unless they name their credential: each is its own identity."""
    fake = FakeTransport().route("GET", "/v1/x", httpx2.Response(200, content=b"private"))
    first, second = seeded(), seeded()

    await client(fake, auth=first, policy=_cached(storage)).get("x", expect=as_bytes)
    await client(fake, auth=second, policy=_cached(storage)).get("x", expect=as_bytes)

    assert len(fake.calls) == len((first, second))


def test_the_cassette_filter_withholds_what_the_real_auths_stamp() -> None:
    """Filter the header ``StaticHeaderAuth`` stamps, ``QueryParamAuth``'s parameter and the bearer's header."""
    auths = (StaticHeaderAuth({"X-Api-Key": "k"}), QueryParamAuth({"appid": "k"}), seeded())

    cassettes = CassetteFilter(auths=auths)

    assert {"x-api-key", "authorization"} <= set(cassettes.filter_headers)
    assert "appid" in cassettes.filter_query_parameters
