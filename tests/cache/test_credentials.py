# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The cache keeps credentials out of storage, and each credential's entries to itself.

Isolation is observed from the network leg (a hit never reaches it), and "nothing stored" by reading every byte of the
real SQLite storage's rows. The auth here is a test-local :class:`~headgate._core.CredentialStamper`; the built-in
auths implement the protocol themselves, and are exercised when they do.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import string
from typing import TYPE_CHECKING, override

import anysqlite
import httpx2
import pytest
from hishel import AsyncSqliteStorage
from hypothesis import given, settings
from hypothesis import strategies as st

from headgate import ClientConfigurationError, HttpOptions, StaticHeaderAuth, TransportPolicy
from headgate._core import CredentialStamper
from headgate.cache import HttpCache, Partition
from headgate.cache._credentials import CredentialScope, credential_scope
from headgate.ratelimit import UNLIMITED
from tests.cache._helpers import cache_transport, series_policy
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

_URL = "https://x/series?id=A"
_HEADER = "X-Api-Key"
_PARAM = "sig"


class _KeyAuth(httpx2.Auth):
    """Stamp a key header and a signature param, and declare both: a minimal :class:`CredentialStamper`."""

    def __init__(self, key: str, *, signature: str = "s") -> None:
        """Send *key* in :data:`_HEADER` and *signature* in :data:`_PARAM`; the identity is the key alone."""
        self._key = key
        self._signature = signature

    @property
    def credential_headers(self) -> frozenset[str]:
        """Return the header this auth sets."""
        return frozenset({_HEADER})

    @property
    def credential_params(self) -> frozenset[str]:
        """Return the param this auth sets."""
        return frozenset({_PARAM})

    def credential_identity(self) -> bytes:
        """Return the key."""
        return self._key.encode()

    @override
    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        """Stamp the header and the param."""
        request.headers.setdefault(_HEADER, self._key)
        request.url = request.url.copy_merge_params({_PARAM: self._signature})
        yield request


class _OpaqueAuth(httpx2.Auth):
    """An auth that stamps a header but declares nothing about it."""

    @override
    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        """Stamp a credential the cache cannot know about."""
        request.headers["X-Opaque"] = "opaque-secret"
        yield request


class _Recorder(httpx2.AsyncBaseTransport):
    """The network leg: records the raw URL and raw headers of every request, and answers 200."""

    def __init__(self) -> None:
        """Start with no requests."""
        self.sent: list[tuple[bytes, list[tuple[bytes, bytes]]]] = []

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent.append((request.url.raw_path, list(request.headers.raw)))
        return httpx2.Response(httpx2.codes.OK, content=b"body")


def _client(
    network: httpx2.AsyncBaseTransport,
    storage: AsyncSqliteStorage,
    auth: httpx2.Auth | None,
    *,
    partition: Partition = Partition.CREDENTIAL,
    headers: Mapping[str, str] | None = None,
) -> StubClient:
    """Build a client caching ``/series`` into *storage*, with *auth* and *partition*."""
    return StubClient(
        base_url="https://x",
        rate=UNLIMITED,
        auth=auth,
        policy=TransportPolicy(cache=HttpCache(policy=series_policy(), storage=storage, partition=partition)),
        http=HttpOptions(transport=network, headers=headers or {}),
    )


#: Every row the SQLite storage holds: entries (request, response head, metadata) and body chunks.
_ALL_ROWS = ("SELECT * FROM entries", "SELECT * FROM streams")


async def _stored_bytes(connection: anysqlite.Connection) -> bytes:
    """Return every value of every row the SQLite storage holds, as bytes."""
    values: list[bytes] = []
    for query in _ALL_ROWS:
        cursor = await connection.execute(query)
        for row in await cursor.fetchall():
            values.extend(value if isinstance(value, bytes) else str(value).encode() for value in row)
    return b"\n".join(values)


# Isolation between credentials.


def test_the_test_auth_is_a_credential_stamper() -> None:
    """Check that the test auth satisfies the protocol the cache reads, so the tests below mean what they say."""
    assert isinstance(_KeyAuth("k"), CredentialStamper)
    assert not isinstance(_OpaqueAuth(), CredentialStamper)


async def test_two_credentials_on_one_storage_never_see_each_others_entries(storage: AsyncSqliteStorage) -> None:
    """Each credential stores and hits its own entry for one URL, however the requests interleave."""
    network = _Recorder()
    alice = _client(network, storage, _KeyAuth("alice"))
    bob = _client(network, storage, _KeyAuth("bob"))

    await alice.get(_URL, expect=RESPONSE)
    bob_first = await bob.get(_URL, expect=RESPONSE)
    assert len(network.sent) == len(("alice", "bob")), "bob missed alice's entry"
    assert bob_first.extensions.get("hishel_from_cache") is not True

    alice_again = await alice.get(_URL, expect=RESPONSE)
    bob_again = await bob.get(_URL, expect=RESPONSE)
    assert len(network.sent) == len(("alice", "bob")), "each hit its own entry"
    assert alice_again.extensions.get("hishel_from_cache") is True
    assert bob_again.extensions.get("hishel_from_cache") is True


async def test_one_credential_shares_its_entries_between_clients(storage: AsyncSqliteStorage) -> None:
    """Two clients with the same credential share entries, whatever per-request value the auth stamps beside it."""
    network = _Recorder()
    await _client(network, storage, _KeyAuth("alice", signature="first")).get(_URL, expect=RESPONSE)

    hit = await _client(network, storage, _KeyAuth("alice", signature="second")).get(_URL, expect=RESPONSE)

    assert len(network.sent) == 1
    assert hit.extensions.get("hishel_from_cache") is True


async def test_an_undeclared_credential_partitions_too(storage: AsyncSqliteStorage) -> None:
    """A client with no auth but its own ``Authorization`` header gets entries of its own per value."""
    network = _Recorder()
    first = _client(network, storage, None, headers={"Authorization": "Bearer one"})
    second = _client(network, storage, None, headers={"Authorization": "Bearer two"})

    await first.get(_URL, expect=RESPONSE)
    await second.get(_URL, expect=RESPONSE)
    await first.get(_URL, expect=RESPONSE)

    assert len(network.sent) == len(("one", "two"))


async def test_a_shared_partition_serves_every_credential(storage: AsyncSqliteStorage) -> None:
    """Under ``Partition.SHARED`` an entry one credential stored is a hit for another."""
    network = _Recorder()
    await _client(network, storage, _KeyAuth("alice"), partition=Partition.SHARED).get(_URL, expect=RESPONSE)

    hit = await _client(network, storage, _KeyAuth("bob"), partition=Partition.SHARED).get(_URL, expect=RESPONSE)

    assert len(network.sent) == 1
    assert hit.extensions.get("hishel_from_cache") is True


async def test_an_anonymous_client_and_a_shared_one_share_a_namespace(storage: AsyncSqliteStorage) -> None:
    """A request with no credential at all is stored unlabelled, where a shared client finds it."""
    network = _Recorder()
    await _client(network, storage, None).get(_URL, expect=RESPONSE)

    hit = await _client(network, storage, _KeyAuth("bob"), partition=Partition.SHARED).get(_URL, expect=RESPONSE)

    assert hit.extensions.get("hishel_from_cache") is True


# Refusing what cannot be partitioned.


def test_an_auth_that_declares_nothing_is_refused_a_credential_partition(storage: AsyncSqliteStorage) -> None:
    """A cache partitioned by credential cannot work with an auth that declares nothing, so the client is refused."""
    with pytest.raises(ClientConfigurationError, match=r"_OpaqueAuth does not declare its credentials.*SHARED"):
        _client(_Recorder(), storage, _OpaqueAuth())


async def test_an_auth_that_declares_nothing_may_share_explicitly(storage: AsyncSqliteStorage) -> None:
    """``Partition.SHARED`` is the explicit opt-in for such an auth: the client builds and caches."""
    network = _Recorder()
    client = _client(network, storage, _OpaqueAuth(), partition=Partition.SHARED)

    await client.get(_URL, expect=RESPONSE)
    await client.get(_URL, expect=RESPONSE)

    assert len(network.sent) == 1


def test_a_built_in_auth_partitions_once_it_declares_its_credentials(storage: AsyncSqliteStorage) -> None:
    """``StaticHeaderAuth`` gets a credential partition when it implements the protocol, and is refused otherwise."""
    cache = HttpCache(policy=series_policy(), storage=storage)
    auth = StaticHeaderAuth({"X-Api-Key": "k"})
    if isinstance(auth, CredentialStamper):
        assert isinstance(credential_scope(cache, auth), CredentialScope)
    else:
        with pytest.raises(ClientConfigurationError):
            credential_scope(cache, auth)


# What the network gets, and what the storage keeps.


async def test_the_network_gets_the_request_exactly_as_an_uncached_client_sends_it(storage: AsyncSqliteStorage) -> None:
    """Every credential the cache hid from hishel goes back, in its place, byte for byte."""
    headers = {"Authorization": "Bearer t", "Cookie": "session=c", "Proxy-Authorization": "Basic p"}
    url = "https://x/series?api_key=k1&id=1&api_key=k2"
    cached, uncached = _Recorder(), _Recorder()

    await _client(cached, storage, _KeyAuth("alice"), headers=headers).get(url, expect=RESPONSE)
    await StubClient(
        base_url="https://x",
        rate=UNLIMITED,
        auth=_KeyAuth("alice"),
        http=HttpOptions(transport=uncached, headers=headers),
    ).get(url, expect=RESPONSE)

    assert cached.sent == uncached.sent
    assert b"api_key=k1" in cached.sent[0][0]


_SECRETS = st.text(alphabet=string.ascii_letters + string.digits, min_size=16, max_size=32)

#: One value per kind of credential: the auth's key and signature, a bearer, a cookie, a query key and a password.
type _Secrets = tuple[str, str, str, str, str, str]


@settings(max_examples=25, deadline=None)
@given(st.tuples(_SECRETS, _SECRETS, _SECRETS, _SECRETS, _SECRETS, _SECRETS))
def test_no_credential_ever_reaches_the_stored_rows(secrets: _Secrets) -> None:
    """Whatever the credential values, no byte of any stored row carries one, and the entry is still a hit."""
    sent, stored = asyncio.run(_store_and_inspect(secrets))

    assert sent == 1, "the second request hit the stored entry"
    assert stored, "the response was stored"
    for secret in secrets:
        assert secret.encode() not in stored


async def _store_and_inspect(secrets: _Secrets) -> tuple[int, bytes]:
    """Send one GET twice with every credential in *secrets*; return the network sends and the stored bytes."""
    key, signature, bearer, cookie, query_key, password = secrets
    connection = await anysqlite.connect(":memory:")
    storage = AsyncSqliteStorage(connection=connection)
    network = _Recorder()
    transport = cache_transport(storage, network, policy=series_policy(), scope=CredentialScope(_KeyAuth(key)))
    try:
        for _ in range(2):
            request = httpx2.Request(
                "GET",
                f"https://user:{password}@x/series?id=A&api_key={query_key}&{_PARAM}={signature}",
                headers={"Authorization": f"Bearer {bearer}", "Cookie": f"s={cookie}", _HEADER: key},
            )
            response = await transport.handle_async_request(request)
            await response.aread()
        return len(network.sent), await _stored_bytes(connection)
    finally:
        await storage.close()


# The view hishel gets, request by request.


def _label(scope: CredentialScope, url: str, headers: Mapping[str, str] | None = None) -> str | None:
    """Return the partition label *scope* gives a GET of *url* with *headers*."""
    stored, _ = scope.view(httpx2.Request("GET", url, headers=headers))
    return stored.fragment or None


def test_the_view_drops_every_credential_and_keeps_the_rest_in_order() -> None:
    """Credential headers, params and userinfo are gone from the view; every other header and param keeps its place."""
    request = httpx2.Request(
        "GET",
        "https://u:pw@x/series?b=2&api_key=k&sig=s&a=1#frag",
        headers=[("Accept", "a"), ("Authorization", "t"), ("X-Api-Key", "k"), ("Cookie", "c"), ("X-Trace", "1")],
    )

    stored, headers = CredentialScope(_KeyAuth("k"), partition=Partition.SHARED).view(request)

    assert str(stored) == "https://x/series?b=2&a=1"
    assert [name for name, _ in headers.multi_items()] == ["host", "accept", "x-trace"]


def test_a_view_without_credentials_leaves_the_url_unlabelled() -> None:
    """A request carrying no credential keeps its URL, query untouched, and gets no partition label."""
    url = "https://x/series?b=2&a=1"

    stored, _ = CredentialScope().view(httpx2.Request("GET", url))

    assert str(stored) == url


def test_labels_separate_credentials_and_follow_the_identity() -> None:
    """One identity labels alike whatever the auth stamps per request; another identity, or an extra credential, not."""
    alice = CredentialScope(_KeyAuth("alice"))
    bob = CredentialScope(_KeyAuth("bob"))

    assert _label(alice, _URL) == _label(CredentialScope(_KeyAuth("alice")), _URL, {_HEADER: "rotated"})
    assert _label(alice, _URL) != _label(bob, _URL)
    assert _label(alice, _URL) != _label(alice, _URL, {"Authorization": "Bearer extra"})
    assert _label(CredentialScope(), _URL, {"Cookie": "a"}) != _label(CredentialScope(), _URL, {"Cookie": "b"})
    assert _label(CredentialScope(), "https://a:1@x/") != _label(CredentialScope(), "https://a:2@x/")
    assert _label(CredentialScope(), "https://x/?api_key=1") != _label(CredentialScope(), "https://x/?api_key=2")


def test_a_shared_scope_never_labels() -> None:
    """Under ``Partition.SHARED`` no request is labelled, whatever credential it carries."""
    shared = CredentialScope(_KeyAuth("alice"), partition=Partition.SHARED)

    assert _label(shared, _URL, {"Authorization": "Bearer t"}) is None


def test_a_label_is_a_keyed_hash_not_the_credential() -> None:
    """The label carries no credential byte, and is an HMAC-SHA256 hex digest behind a fixed prefix."""
    label = _label(CredentialScope(_KeyAuth("alice-secret-key")), _URL)

    assert label is not None
    assert "alice" not in label
    prefix, digest = label.rsplit("-", 1)
    assert prefix == "headgate-partition"
    assert len(digest) == len(hashlib.sha256().hexdigest())


@pytest.mark.parametrize("ttl", [0.0, -1.0, math.inf, math.nan])
def test_a_cache_refuses_a_ttl_no_entry_could_live_by(ttl: float, storage: AsyncSqliteStorage) -> None:
    """``HttpCache`` refuses a TTL that is not a positive finite number of seconds."""
    with pytest.raises(ValueError, match="ttl must be a positive finite number"):
        HttpCache(policy=series_policy(), storage=storage, ttl=ttl)
