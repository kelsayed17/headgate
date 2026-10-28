# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The cache keeps credentials out of storage, and each credential's entries to itself.

Isolation is observed from the network leg (a hit never reaches it), and "nothing stored" by reading every byte of the
real SQLite storage's rows. The auth here is a test-local :class:`~headgate._core.CredentialStamper`; the built-in
auths implement the protocol themselves, and are exercised when they do.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import math
import string
from typing import TYPE_CHECKING, ClassVar, override

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
from tests.cache._helpers import accept, cache_transport, series_policy
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Mapping

    from headgate import Next

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


class _KeyMiddleware:
    """A middleware that stamps a key header and declares it, as a :class:`~headgate._core.CredentialCarrier`."""

    credential_headers: ClassVar = frozenset({_HEADER})
    credential_params: ClassVar = frozenset[str]()

    def __init__(self, key: str) -> None:
        """Send *key* in :data:`_HEADER` on every attempt."""
        self._key = key

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Stamp the key, then send."""
        request.headers[_HEADER] = self._key
        return await call_next(request)


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
    http: HttpOptions | None = None,
) -> StubClient:
    """Build a client caching ``/series`` into *storage*, with *auth* and *partition*, and *http* over *network*."""
    return StubClient(
        base_url="https://x",
        rate=UNLIMITED,
        auth=auth,
        policy=TransportPolicy(cache=HttpCache(policy=series_policy(), storage=storage, partition=partition)),
        http=dataclasses.replace(http or HttpOptions(), transport=network),
    )


#: Every row the SQLite storage holds: entries (request, response head, metadata) and body chunks.
_ALL_ROWS = ("SELECT * FROM entries", "SELECT * FROM streams")


@pytest.fixture
async def connection() -> anysqlite.Connection:
    """Return the in-memory SQLite connection under :func:`inspected`, so a test can read every stored row."""
    return await anysqlite.connect(":memory:")


@pytest.fixture
async def inspected(connection: anysqlite.Connection) -> AsyncIterator[AsyncSqliteStorage]:
    """Yield a storage on :func:`connection`, closed (with the connection) after the test."""
    store = AsyncSqliteStorage(connection=connection)
    try:
        yield store
    finally:
        await store.close()


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
    first = _client(network, storage, None, http=HttpOptions(headers={"Authorization": "Bearer one"}))
    second = _client(network, storage, None, http=HttpOptions(headers={"Authorization": "Bearer two"}))

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
    """A request with no credential at all is stored unlabeled, where a shared client finds it."""
    network = _Recorder()
    await _client(network, storage, None).get(_URL, expect=RESPONSE)

    hit = await _client(network, storage, _KeyAuth("bob"), partition=Partition.SHARED).get(_URL, expect=RESPONSE)

    assert hit.extensions.get("hishel_from_cache") is True


async def test_a_middlewares_declared_credential_partitions_and_is_never_stored(
    inspected: AsyncSqliteStorage, connection: anysqlite.Connection
) -> None:
    """A key a middleware stamps and declares keeps each key's entries apart, and no stored row carries it."""
    network = _Recorder()
    alice = _client(network, inspected, None, http=HttpOptions(middleware=(_KeyMiddleware("alice-middleware-key"),)))
    bob = _client(network, inspected, None, http=HttpOptions(middleware=(_KeyMiddleware("bob-middleware-key"),)))

    await alice.get(_URL, expect=RESPONSE)
    await bob.get(_URL, expect=RESPONSE)
    hit = await alice.get(_URL, expect=RESPONSE)

    assert len(network.sent) == len(("alice", "bob")), "bob missed alice's entry"
    assert hit.extensions.get("hishel_from_cache") is True
    stored = await _stored_bytes(connection)
    assert b"middleware-key" not in stored


async def test_a_client_wide_header_partitions_and_is_never_stored(
    inspected: AsyncSqliteStorage, connection: anysqlite.Connection
) -> None:
    """A key in ``HttpOptions(headers=...)`` keeps each key's entries apart, and no stored row carries it."""
    network = _Recorder()
    alice = _client(network, inspected, None, http=HttpOptions(headers={_HEADER: "alice-client-key"}))
    bob = _client(network, inspected, None, http=HttpOptions(headers={_HEADER: "bob-client-key"}))

    await alice.get(_URL, expect=RESPONSE)
    await bob.get(_URL, expect=RESPONSE)
    hit = await alice.get(_URL, expect=RESPONSE)

    assert len(network.sent) == len(("alice", "bob")), "bob missed alice's entry"
    assert hit.extensions.get("hishel_from_cache") is True
    assert b"client-key" not in await _stored_bytes(connection)


async def test_a_shared_partition_stores_no_header_value_as_sent(
    inspected: AsyncSqliteStorage, connection: anysqlite.Connection
) -> None:
    """Under ``Partition.SHARED`` an undeclared auth's header is not stored, nor a client-wide or per-call one."""
    network = _Recorder()
    http = HttpOptions(headers={"X-Static": "static-1"})
    client = _client(network, inspected, _OpaqueAuth(), partition=Partition.SHARED, http=http)

    await client.get(_URL, expect=RESPONSE, headers={"X-Call": "per-call-1"})
    hit = await client.get(_URL, expect=RESPONSE, headers={"X-Call": "per-call-1"})

    assert hit.extensions.get("hishel_from_cache") is True
    stored = await _stored_bytes(connection)
    assert all(value not in stored for value in (b"opaque-secret", b"static-1", b"per-call-1"))


async def test_a_header_a_response_varies_on_still_tells_entries_apart(storage: AsyncSqliteStorage) -> None:
    """The stored request headers are digests, so ``Vary`` still matches a request only to its own variant."""
    sent: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.headers["Accept-Language"])
        return httpx2.Response(httpx2.codes.OK, headers={"Vary": "Accept-Language"}, text=sent[-1])

    english = _client(httpx2.MockTransport(answer), storage, None, partition=Partition.SHARED)
    french = _client(httpx2.MockTransport(answer), storage, None, partition=Partition.SHARED)

    await english.get(_URL, expect=RESPONSE, headers={"Accept-Language": "en"})
    answer_fr = await french.get(_URL, expect=RESPONSE, headers={"Accept-Language": "fr"})
    hit_en = await english.get(_URL, expect=RESPONSE, headers={"Accept-Language": "en"})

    assert sent == ["en", "fr"]
    assert (answer_fr.text, hit_en.text) == ("fr", "en")
    assert hit_en.extensions.get("hishel_from_cache") is True


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


def test_a_built_in_auth_gets_a_credential_partition(storage: AsyncSqliteStorage) -> None:
    """``StaticHeaderAuth`` declares its credentials, so a credential-partitioned cache accepts it."""
    cache = HttpCache(policy=series_policy(), storage=storage)

    assert isinstance(credential_scope(cache, StaticHeaderAuth({"X-Api-Key": "k"})), CredentialScope)


# What the network gets, and what the storage keeps.


async def test_the_network_gets_the_request_exactly_as_an_uncached_client_sends_it(storage: AsyncSqliteStorage) -> None:
    """Every credential the cache hid from hishel goes back, in its place, byte for byte."""
    headers = {"Authorization": "Bearer t", "Cookie": "session=c", "Proxy-Authorization": "Basic p"}
    url = "https://x/series?api_key=k1&id=1&api_key=k2"
    cached, uncached = _Recorder(), _Recorder()

    await _client(cached, storage, _KeyAuth("alice"), http=HttpOptions(headers=headers)).get(url, expect=RESPONSE)
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
            await accept(response)
        return len(network.sent), await _stored_bytes(connection)
    finally:
        await storage.close()


class _SetsCookie(httpx2.AsyncBaseTransport):
    """The network leg: answers every request 200 with a session cookie, and counts them."""

    def __init__(self) -> None:
        """Start with no requests."""
        self.sent = 0

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        cookies = [("Set-Cookie", f"session={_SESSION}; Secure"), ("Set-Cookie", "theme=dark")]
        return httpx2.Response(httpx2.codes.OK, headers=[*cookies, ("X-Kept", "1")], content=b"body")


#: The session a response sets: it must reach the caller and never the storage.
_SESSION = "s3ss10n-cookie-value"


async def test_a_set_cookie_is_handed_to_the_caller_and_never_stored() -> None:
    """Give the fetching request every ``Set-Cookie`` it got; store none, so a hit carries none."""
    connection = await anysqlite.connect(":memory:")
    storage = AsyncSqliteStorage(connection=connection)
    network = _SetsCookie()
    transport = cache_transport(storage, network, policy=series_policy())
    try:
        fetched = await transport.handle_async_request(httpx2.Request("GET", _URL))
        await accept(fetched)
        hit = await transport.handle_async_request(httpx2.Request("GET", _URL))
        await hit.aread()
        stored = await _stored_bytes(connection)
    finally:
        await storage.close()

    assert network.sent == 1, "the second request hit the stored entry"
    assert fetched.headers.get_list("set-cookie") == [f"session={_SESSION}; Secure", "theme=dark"]
    assert (fetched.headers["x-kept"], hit.headers["x-kept"]) == ("1", "1")
    assert hit.extensions.get("hishel_from_cache") is True
    assert "set-cookie" not in hit.headers
    assert b"body" in stored
    assert _SESSION.encode() not in stored
    assert b"theme" not in stored


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

    assert str(stored.copy_with(fragment=None)) == "https://x/series?b=2&a=1"
    assert stored.fragment.startswith("headgate-partition-"), "the undeclared api_key labels it, the fragment does not"
    assert [name for name, _ in headers.multi_items()] == ["host", "accept", "x-trace"]


def test_a_view_without_credentials_leaves_the_url_unlabeled() -> None:
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


class _OtherKeyAuth(_KeyAuth):
    """The same credential under another auth class."""


def test_labels_separate_auth_classes_with_the_same_identity() -> None:
    """Partition by the auth's class as well as its identity: two auths that happen to share an identity never share."""
    assert _label(CredentialScope(_KeyAuth("k")), _URL) != _label(CredentialScope(_OtherKeyAuth("k")), _URL)


def test_a_shared_scope_never_labels_by_credential() -> None:
    """Under ``Partition.SHARED`` no request is labeled for a credential header, cookie or userinfo it carries."""
    shared = CredentialScope(_KeyAuth("alice"), partition=Partition.SHARED)

    assert _label(shared, _URL, {"Authorization": "Bearer t", "Cookie": "c"}) is None
    assert _label(shared, "https://a:1@x/series") is None


def test_a_shared_scope_keeps_secret_named_parameters_apart_by_value() -> None:
    """Under ``Partition.SHARED`` a parameter named like a credential is still told apart by its value.

    ``key`` is a well-known secret name, but ``/config?key=theme`` and ``?key=locale`` are different resources: dropped
    without a trace they would share one entry. A parameter the auth declares is its credential, and is not.
    """
    shared = CredentialScope(_KeyAuth("alice"), partition=Partition.SHARED)

    theme, locale = _label(shared, "https://x/config?key=theme"), _label(shared, "https://x/config?key=locale")
    assert None not in (theme, locale)
    assert theme != locale
    assert _label(shared, f"{_URL}&{_PARAM}=one") is None


def test_the_view_digests_every_header_it_keeps() -> None:
    """A kept header's value is a keyed digest, the same for the same value, so it can be matched but not read."""
    scope = CredentialScope()

    _, first = scope.view(httpx2.Request("GET", _URL, headers={"X-Key": "value-one"}))
    _, again = scope.view(httpx2.Request("GET", _URL, headers={"X-Key": "value-one"}))
    _, other = scope.view(httpx2.Request("GET", _URL, headers={"X-Key": "value-two"}))

    assert "value-one" not in first["x-key"]
    assert first["x-key"] == again["x-key"] != other["x-key"]


class _IdentifiedSigner(_KeyMiddleware):
    """A middleware whose stamp changes per request, and which names the credential it signs with."""

    def credential_identity(self) -> bytes:
        """Return the signing key."""
        return self._key.encode()


def test_labels_follow_a_middlewares_values_and_a_client_wide_header() -> None:
    """A middleware's declared header and a client-wide header label by value; the auth's own stays its identity."""
    carrier = CredentialScope(carriers=(_KeyMiddleware("k"),))
    client_wide = CredentialScope(client_headers=("X-Static",))

    assert _label(carrier, _URL, {_HEADER: "a"}) != _label(carrier, _URL, {_HEADER: "b"})
    assert _label(client_wide, _URL, {"X-Static": "a"}) != _label(client_wide, _URL, {"X-Static": "b"})
    assert _label(client_wide, _URL) is None


def test_a_middleware_that_names_its_credential_labels_by_that() -> None:
    """A middleware that is a ``CredentialStamper`` labels by its identity, so a per-request signature still hits."""
    alice = CredentialScope(carriers=(_IdentifiedSigner("alice"),))
    bob = CredentialScope(carriers=(_IdentifiedSigner("bob"),))

    assert _label(alice, _URL, {_HEADER: "signature-1"}) == _label(alice, _URL, {_HEADER: "signature-2"})
    assert _label(alice, _URL, {_HEADER: "signature-1"}) != _label(bob, _URL, {_HEADER: "signature-1"})


class _Declares:
    """A credential source that declares one header and one param, under *identity*: a bare ``CredentialStamper``."""

    def __init__(self, header: str, param: str, identity: bytes) -> None:
        """Declare *header* and *param*, and name the credential *identity*."""
        self.credential_headers = frozenset({header})
        self.credential_params = frozenset({param})
        self._identity = identity

    def credential_identity(self) -> bytes:
        """Return the identity."""
        return self._identity


def test_every_sources_credential_is_dropped_and_labels_by_its_identity() -> None:
    """Drop what the auth and each middleware declare, and label by each identity rather than by what each stamps."""
    signer = _Declares("X-Second", "second_part", b"signer")

    def scope(identity: bytes) -> CredentialScope:
        return CredentialScope(_Declares("X-First", "first_part", identity), carriers=(signer,))

    def view(scope: CredentialScope, stamp: str) -> tuple[httpx2.URL, httpx2.Headers]:
        url = f"https://x/series?first_part={stamp}&second_part={stamp}&id=A"
        return scope.view(httpx2.Request("GET", url, headers={"X-First": stamp, "X-Second": stamp}))

    (stored, headers), (again, _) = view(scope(b"alice"), "one"), view(scope(b"alice"), "two")
    other, _ = view(scope(b"bob"), "one")

    assert str(stored.copy_with(fragment=None)) == "https://x/series?id=A"
    assert {"x-first", "x-second"}.isdisjoint(headers)
    assert stored.fragment == again.fragment != other.fragment


def test_a_kept_headers_digest_is_bound_to_its_name() -> None:
    """Digest a kept header's value together with its name, so one value sent under two names is not matched."""
    _, headers = CredentialScope().view(httpx2.Request("GET", _URL, headers={"X-One": "same", "X-Two": "same"}))

    assert headers["x-one"] != headers["x-two"]


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
