# Caching

A client caches when its policy says so: `TransportPolicy(cache=HttpCache(...))`. The cache is
[hishel](https://hishel.com)'s, made safe to share between credentials and to fail. It sits below the retry layer and
above the rate budget, so a hit returns before any retry and spends no rate turn.

```python
from hishel import AsyncSqliteStorage

from headgate import HttpCache, TransportPolicy

cache = HttpCache(
    storage=AsyncSqliteStorage(database_path="api-cache.db"),  # yours to close; many clients may share it
    paths=("/series", "/releases"),  # the GETs it keeps, and only their 200s
    ttl=3600,
)
policy = TransportPolicy(cache=cache)
```

`AsyncSqliteStorage` needs `anysqlite`, which a plain `pip install headgate` does not bring: install
`pip install 'headgate[cache]'` (it pulls `hishel[async]`). `AsyncRedisStorage` needs `hishel[redis]`.

## What is stored, and for how long

`HttpCache(*, storage, ttl=None, partition=Partition.CREDENTIAL, methods=None, paths=None, statuses=None,
policy=None)`:

- **What is stored**: a request whose method is in `methods` (`{"GET"}` by default) and whose path holds one of
  `paths` (every path by default; whole segments, as a `RateRule` matches), answered with a status in `statuses`
  (`{200}` by default). It is stored **even past a `no-store`**, which is the point for a provider that sends no cache
  headers, or wrong ones. An empty collection matches nothing, so `methods=()` stores nothing.
- **`methods` names `GET` and `HEAD` only.** An entry is keyed by its URL, so two POSTs with different bodies would
  share one; any other method raises `ValueError`.
- **`policy`** replaces those three with a hishel `FilterPolicy` of your own, built from `headgate.cache`'s
  `MethodFilter`, `PathFragmentFilter` and `StatusCodeFilter` or your own filters; it cannot be combined with them.
  It is the only way to cache a read-only POST: `FilterPolicy(use_body_key=True, ...)` keys it by its body too.
- **Stored on accept.** A response the policy allows is written to storage only once the call's `expect=` reader has
  accepted it. A call whose reader raises stores nothing, so the cache never serves an answer the client refused:
  a refusal inside a 200 that a reader's `refusal_check` catches, an undecodable body, an undeclared status. A
  declared non-2xx (`Expect(...).on(404, ...)`) is stored when the filters allow its status. A `stream()` has no
  reader, so passing its head checks accepts it. A write that fails then is counted as a degradation; the call keeps
  its answer.
- **`ttl`** is the client's default lifetime in seconds; a call's `cache_ttl=` overrides it.
- **TTL, not revalidation.** An entry is served until its lifetime ends and is never revalidated with the origin. For
  revalidation, use [conditional reads](#conditional-reads).
- The body is held up to `HttpOptions.max_response_bytes`, and a body past it raises `ResponseTooLargeError` as it would
  uncached. A body past the cap is not stored, not even for a `stream()`.

## Partitions

**An entry is visible only to requests made with the same credential** (`Partition.CREDENTIAL`, the default), so clients
with different keys can share one storage. The partition is an HMAC-SHA256, under a key drawn at random once per
process, of the auth's class and `credential_identity()`, of any other credential the request carries (what a
[middleware](configuration.md#middleware) declares in `credential_headers` and `credential_params`, a caller's own
`Authorization`, a cookie, an `api_key` in the URL), and of every header set in `HttpOptions(headers=...)`, where a
static key is often configured. A middleware whose signature changes on every request implements
`credential_identity()` too, and is then partitioned by that, as an auth is. Labels can be neither reversed nor
compared across processes, so credential-partitioned entries in a persistent storage are not found again after a
restart; they expire by their TTL. A request with no credential at all is unpartitioned.

A `CachedTokenAuth` partitions by the long-lived credential, not the short-lived token, so a refresh keeps its entries.
By default each instance is its own identity; override `credential_identity()` to return the credential's secret
material (it is hashed, never stored) so two instances holding it share entries. See [auth](auth.md#cachedtokenauth).

An auth that does not declare its credentials (not a `CredentialStamper`) is refused when the client is built, as
`ClientConfigurationError`, since nothing could keep its entries apart. `HttpCache(partition=Partition.SHARED)` is the
opt-in for public data: every client on the storage sees every entry, whatever its credential. One thing still keeps
entries apart there: a query parameter with a well-known secret name that the auth does not declare, by its value,
since `/config?key=theme` and `?key=locale` are different resources.

## Credentials are never stored

hishel persists the request with each entry, so it is handed a view of the request without `Authorization`,
`Proxy-Authorization`, `Cookie`, every header and query parameter the auth or a middleware declares, every well-known
secret query parameter, and the URL's userinfo. Every other header is stored as a keyed digest of its value, so a key
in a header nothing declares (a client-wide one, a call's own, an undeclared auth's under `Partition.SHARED`) is not
stored either, while a response's `Vary` still matches only requests with the same values. The digest key is per
process too, so an entry that varies on a request header is not found again after a restart. The real request goes to
the network unchanged. A response's `Set-Cookie` is never stored either; the request that fetched it still receives
it.

## Identical requests share one download

hishel has no request coalescing, so an identical GET arriving while the first is still being fetched would miss too.
headgate makes it wait and gives it a copy of the first one's response (a hit, a miss or a degraded fetch). Identical
means the same URL, every header, the same timeouts and the same retry policy; the timeouts compared are the ones the
call asked for, not the ones its deadline cut, so calls with different time left still share. A timeout that only the
first request's deadline cut short is not passed on: each waiting request then fetches its own. A network failure fails
every waiting request from the one attempt, each with its own copy of the error, and each caller's retry layer then
retries (and shares again). Only the request that fetched counts toward the [breaker](breaker.md). A body past the size
cap is not held, so each waiting request fetches its own. POSTs are never shared.

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
from headgate.cache import Fresh, NotModified, Validator, conditional
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
