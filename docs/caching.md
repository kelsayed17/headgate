# Caching

A client caches when its policy says so: `TransportPolicy(cache=HttpCache(...))`. The cache is
[hishel](https://hishel.com)'s, made safe to share between credentials and to fail. It sits below the retry layer and
above the rate budget, so a hit returns before any retry and spends no rate turn.

```python
from hishel import AsyncSqliteStorage, FilterPolicy

from headgate import HttpCache, MethodFilter, PathFragmentFilter, StatusCodeFilter, TransportPolicy

cache = HttpCache(
    policy=FilterPolicy(
        request_filters=[MethodFilter(frozenset({"GET"})), PathFragmentFilter(("/series", "/releases"))],
        response_filters=[StatusCodeFilter(frozenset({200}))],
    ),
    storage=AsyncSqliteStorage(database_path="api-cache.db"),  # yours to close; many clients may share it
    ttl=3600,
)
policy = TransportPolicy(cache=cache)
```

`AsyncSqliteStorage` needs `anysqlite`, which a plain `pip install headgate` does not bring: install
`pip install 'headgate[cache]'` (it pulls `hishel[async]`). `AsyncRedisStorage` needs `hishel[redis]`.

## What is stored, and for how long

`HttpCache(policy, storage, ttl=None, partition=Partition.CREDENTIAL)`:

- **`policy`** is a hishel `FilterPolicy`. headgate supplies three filters: `PathFragmentFilter(fragments)` (the URL path
  contains one of them), `MethodFilter(methods)` and `StatusCodeFilter(codes)`. A `FilterPolicy` stores whenever a
  request filter matches and an allowed status comes back, **even past a `no-store`**, which is the point for a
  provider that sends no cache headers, or wrong ones. Store only 200s, so an error body never poisons the cache.
  Body-keyed POST caching is hishel's own `FilterPolicy.use_body_key`.
- **`ttl`** is the client's default lifetime in seconds; a call's `cache_ttl=` overrides it.
- **TTL, not revalidation.** An entry is served until its lifetime ends and is never revalidated with the origin. For
  revalidation, use [conditional reads](#conditional-reads).
- The body is held up to `HttpOptions.max_response_bytes`, and a body past it raises `ResponseTooLargeError` as it would
  uncached.

## Partitions

**An entry is visible only to requests made with the same credential** (`Partition.CREDENTIAL`, the default), so clients
with different keys can share one storage. The partition is an HMAC-SHA256, under a key drawn at random once per
process, of the auth's class and `credential_identity()`, and of any other credential the request carries (a caller's
own `Authorization`, a cookie, an `api_key` in the URL). Labels can be neither reversed nor compared across processes,
so credential-partitioned entries in a persistent storage are not found again after a restart; they expire by their
TTL. A request with no credential at all is unpartitioned.

A `CachedTokenAuth` partitions by the long-lived credential, not the short-lived token, so a refresh keeps its entries.
By default each instance is its own identity; override `credential_identity()` to return the credential's secret
material (it is hashed, never stored) so two instances holding it share entries. See [auth](auth.md#cachedtokenauth).

An auth that does not declare its credentials (not a `CredentialStamper`) is refused when the client is built, as
`ClientConfigurationError`, since nothing could keep its entries apart. `HttpCache(partition=Partition.SHARED)` is the
opt-in for public data: every client on the storage sees every entry, whatever its credential.

## Credentials are never stored

hishel persists the request with each entry, so it is handed a view of the request without `Authorization`,
`Proxy-Authorization`, `Cookie`, every header and query parameter the auth declares, every well-known secret query
parameter, and the URL's userinfo. The real request goes to the network unchanged. A response's `Set-Cookie` is never
stored either; the request that fetched it still receives it.

## Identical requests share one download

hishel has no request coalescing, so an identical GET arriving while the first is still being fetched would miss too.
headgate makes it wait and gives it a copy of the first one's response (a hit, a miss or a degraded fetch). Identical
means the same URL, every header, the same requested timeouts (before fitting them to each call's deadline), and the
same retry policy. Explicit middleware timeout changes remain part of that identity; fitting a later attempt
preserves those overrides rather than restoring the original client defaults. Each call still has its own deadline: expiring or cancelling one does not cancel another. A timeout
in a phase shortened by the fetching call's deadline is not handed to its waiters; the next waiter fetches for those
that still have time. An unchanged phase timeout, or another network failure, fails every waiting
request from the one attempt, each with its own copy of the error, and each caller's retry layer then retries (and
shares again). Only the request that fetched counts toward the [breaker](breaker.md). A body past the size cap is not
held, so each waiting request fetches its own. POSTs are never shared.

## Storage faults

A storage that fails (a full disk, an unreachable Redis) never fails a call that would otherwise succeed: the request
is redone on the network, reported as a `CacheResult` event with outcome `degraded`, and counted in
`headgate.health.cache_degradations()`. Nothing a caller can see repeats the backend's message, which can carry a DSN
and its password; only the exception's type is named. Anything else raised inside hishel is a bug and propagates.

The storage is never closed by a client: one storage may serve many clients, so whoever created it closes it.

## What the cache means for the rest of the stack

- A hit spends no rate turn and is neither a success nor a failure for the breaker. Admission is still decided before
  the fetch, so an open breaker refuses a call the cache could have answered.
- Every request the cache answers emits `CacheResult` with outcome `hit`, `miss`, `shared` or `degraded`.

## Conditional reads

The caller-driven alternative to a cache: keep the `Validator` (`ETag`, `Last-Modified`) of what you hold and send it
back. The call sends `If-None-Match` / `If-Modified-Since` and returns `Fresh(value, validator)` for a new
representation, or `NotModified(validator)` for a 304. headgate stores nothing; the representation is yours.

```python
from headgate import Fresh, NotModified, Validator, conditional
from headgate.msgspec import decoder


async def refresh(client: CatalogClient, held: Series, validator: Validator | None) -> tuple[Series, Validator]:
    result = await client.get("series/GDP", expect=conditional(decoder(Series), validator=validator))
    match result:
        case Fresh(value=series, validator=new):
            return series, new
        case NotModified(validator=same):
            return held, same
```

Without a validator (the first read) no precondition is sent and the result is always `Fresh`. A precondition the call
sets in `headers=` wins. Behind a `FilterPolicy` cache, a conditional call the cache can answer comes back `Fresh` with
the stored body, without asking the origin.
