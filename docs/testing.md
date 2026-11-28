# Testing a client

`headgate.testing` tests a client through its real stack (retries, the rate budget, the breaker, the error mapping)
with nothing private patched. Two seams make that possible:

- **the network**: `fake_network(transport)` sends every `Client` built in its block on *transport*, in place of the
  network, even one the SDK gave a transport of its own (`HttpOptions(transport=AsyncCurlTransport(...))`). Only the
  network is replaced: the SDK's rate budget, cache, retries, breaker and SSRF guard run over the fake as they would in
  production, and so do the requests a `CachedTokenAuth` mints with. It is the network only: a transport of the SDK's
  that does more than send (one that adds a header, say) is replaced too, so that behavior belongs in middleware.
  The guard resolves names inside the fake network, so a guarded SDK needs no DNS (see
  [names](#names-inside-the-fake-network)). `with fake_network(fake) as net:` yields a `FakeNetwork`: `net.transport`
  is the fake, and `net.clients` the clients built in the block, in order;
- **time**: `TestClock.install()` and `instant_retries()` set the timing the clients built in their block run on.

All three are read when a client is **constructed**, so build the SDK inside them; entering one around a client that
already exists changes nothing, and the block need not stay open while the client is used. Breaker state needs no
seam: each client keeps its own.

<!-- docs-test: run -->
```python
from collections.abc import Iterator

import pytest

from headgate.testing import FakeTransport, Respond, fake_network, instant_retries


@pytest.fixture
def catalog() -> Iterator[CatalogClient]:
    fake = FakeTransport().reply("GET", "/v1/series/GDP", Respond(json=GDP))
    with fake_network(fake), instant_retries():
        yield CatalogClient("test-key")
```

## FakeTransport

A route table of canned replies and a log of every request:

<!-- docs-test: run -->
```python
fake = (
    FakeTransport()
    .reply("GET", "/v1/series/GDP", httpx2.Response(503), httpx2.Response(200, json=GDP))
    .reply("DELETE", "/v1/series/GDP", 204)
)
```

- A route matches the method (case-insensitively) and the path **exactly as sent**: with the client's base path, and
  percent-encoded as a t-string escapes it (`/v1/series/A%2FB`). The query is not part of the match.
- A route's replies are served in order, the last repeating, so a retry sequence is one route.
- A reply is a `Respond` (the step `FaultyTransport` answers with), an `httpx2.Response` built with `content=`, `json=`
  or `text=` (copied for each request), a bare status, or a function of the request returning a response, sync or
  async (the type `Reply` names all four). A function can raise an httpx2 error to simulate a
  network failure.
- `fake.calls` is every request sent, in order, body read; `fake.calls_to(method, path)` filters it; `fake.closed`
  says whether the client closed it.
- A request with no route raises `UnmatchedRequestError`, an `AssertionError` the client neither retries nor maps, naming
  the request and the routes there are.

## Names inside the fake network

A client with the SSRF guard on (`SecurityOptions(block_private_hosts=True)`) resolves each host before it sends.
Inside `fake_network`, the guard resolves through the fake network instead of the system's resolver, and instead of a
`resolver=` the SDK set itself: every name resolves to one public address, so the test makes no DNS query and passes
offline. `FakeTransport.resolve(host, *addresses)` gives a host the answer a test needs, so the guard's refusals can
be tested too: a private address among them is refused with `ForbiddenDestinationError`, and no address at all is a
name that does not resolve, a `TransportError`. The guard's checks still run, so a private IP literal is refused
without any lookup.

<!-- docs-test: run -->
```python
fake = FakeTransport().reply("GET", "/v1/ping", 200).resolve("internal.example.com", "10.0.0.7")
```

## TestClock and instant retries

`TestClock(start=0.0, *, epoch=..., random=...)` is a monotonic clock that moves only when told: `clock.now`,
`clock.advance(seconds)`, and an async `clock.sleep` that records the wait in `clock.sleeps` and advances the clock
instead of waiting. Its wall clock reads `epoch` plus the reading, so a `Retry-After` date agrees with it, and `random`
is the backoff's jitter draw: pass `random=lambda: 1.0` for a full backoff a test can predict.

Everything built inside `clock.install()` runs on it: a client's deadlines, events, retry
backoff and retry budget, the token lifetimes of the `CachedTokenAuth` it is given, a `RateBudget` the block builds
(shared by any number of clients), and a `BreakerRegistry`'s cooldowns. So a test of a 60-second cooldown, or of a
token that expires in an hour, runs in no time. What was built before the block keeps the clock it was built with. A
`CachedTokenAuth` runs on its clients' one timing: giving it to a second client built on another, or to a client on
another timing than the one a token was seeded on, raises `TypeError`, so build its clients and seed it in one
block.

`install()` is `headgate.timing.timing_override(clock.timing)`. The seam is public, so a test kit of your own can
install a clock the same way: a `Timing(clock=..., sleep=..., wall_clock=..., random=...)`, whose four fields satisfy
the `Clock`, `Sleep` and `RandomSource` protocols, set for a block with `timing_override`, and read back with
`current_timing()`. It is the one way in: no budget, registry, breaker or fake network takes a timing of its own, so
build a shared one inside the block whose clock it should run on. A client given a shared `RateBudget`,
`BreakerRegistry` or `RetryBudget` built on another timing raises `TypeError`, as it does for a `CachedTokenAuth` on
another, since one quota, cooldown or retry window cannot run on two clocks: build the shared object and its clients in
one block.

Concurrent sleeps overlap as they would in real time: each sleeper wakes at the reading it began at plus its delay.
The clock moves to the earliest wake after 16 turns of the event loop in which no sleep begins or ends, room for the
tasks that are not sleeping to reach their own sleep. Two calls that back off 30 seconds together both retry at 30, and
a sleep cancelled before it wakes does not move the clock. The clock cannot see what a task is waiting on, so a task
that runs for more turns than that, or waits on real I/O or a thread, is overtaken: its sleep starts at the later
reading.

`instant_retries()` leaves the clock as it is (the real one, or a `TestClock`) and makes every wait return at once, so a
mocked failure is retried exactly as production retries it, without the backoff.

## Breakers

Each client keeps its breakers in a `BreakerRegistry` of its own, unless it is given one to share
(`CallPolicy(breaker=BreakerRegistry(...))`), so failures in one test open no breaker in the next. `breakers_of(client)`
returns that registry (or `None` for a client without a breaker); assert on it with
`headgate.health.breaker_states(registry)`.

## What a client was built with

A client fixes its options in its `__init__`, so a test of its configuration reads them back, from the `Client` the SDK
built, which `fake_network`'s handle holds: `net.clients`, so the test never reaches into the SDK. `cache_of(client)` returns
the `HttpCache` it was built with (or `None`), whose `methods`, `paths` and `statuses` say what it stores.
`budget_for(client, url)` returns the `RateBudget` a request to `url` spends from: that of the first `Route` that
matches it, when that route sets `rate=`, or the default; without a `url`, the default budget, built from the policy's
`rate=`. Neither sends anything. Whether a response is served from the cache shows through the front door too: a
second call that a fake network never sees.

<!-- docs-test: run -->
```python
from headgate.testing import FakeTransport, budget_for, cache_of, fake_network


def test_candles_have_a_budget_of_their_own() -> None:
    with fake_network(FakeTransport()) as net:
        ExchangeClient(api_key="test")
    [client] = net.clients
    assert budget_for(client, "products/BTC-USD/candles") is not budget_for(client)
    assert budget_for(client).rate == Rate.per_second(10)


async def test_only_the_archives_are_cached(storage: AsyncSqliteStorage) -> None:
    fake = FakeTransport().reply("GET", "/archives/2025.zip", 200).reply("GET", "/status", 200)
    with fake_network(fake) as net:
        archive = ArchiveClient(cache_storage=storage)
    async with archive:
        for _ in range(2):
            await archive.archive("2025.zip")
            await archive.status()

    assert len(fake.calls_to("GET", "/archives/2025.zip")) == 1, "the second came from the cache"
    assert len(fake.calls_to("GET", "/status")) == 2, "the status is never cached"
    cache = cache_of(net.clients[0])
    assert cache is not None and cache.ttl == 86_400.0
```

A view made with `with_options()` reads back as the client it was made from.

## A fake walk

A test that stands in for an SDK whose method returns a walk, rather than for its network, builds a `Paginated` of its
own: `Paginated(pages, items_of)`, where `pages` is a function of no arguments that starts the pages afresh each time it
is called (an async generator function, say), and `items_of` returns the items on one page. It is the class
`paginate()` returns, so the code under test iterates, collects and reads pages exactly as it does against the SDK:

<!-- docs-test: run -->
```python
from collections.abc import AsyncIterator

from headgate import Paginated


async def two_pages() -> AsyncIterator[list[Issue]]:
    yield [Issue(number=1, title="first")]
    yield [Issue(number=2, title="second")]


class FakeGitHub:
    def open_issues(self, owner: str, name: str) -> Paginated[Issue]:
        return Paginated(two_pages, lambda page: page)
```

## Fault injection

`FaultyTransport(schedule=[...])` waits out a `Slow` step on the sleep of the timing current when it is built (the test
clock, inside `clock.install()`), answers each send with the next step of its schedule, reports
send progress the way httpx2's own transport does (so the retry layer's "was anything written?" rule sees real facts),
and records each send as a `FaultCall`: the step, method, URL, headers, whether it connected, `bytes_sent`, the
response, whether that response was closed, and the error raised.

| Step | Does |
| --- | --- |
| `ConnectFail(timeout=False)` | raises `ConnectError` (`ConnectTimeout` with `timeout=True`); nothing sent |
| `PoolFull()` | raises `PoolTimeout`; nothing sent |
| `Respond(status_code=200, headers=..., json=..., content=..., retry_after=None)` | answers; `retry_after` adds `Retry-After` in plain digits (whole seconds as delta-seconds, a fraction as a decimal headgate also reads), and a negative or non-finite one raises `ValueError` |
| `DropMidBody(after_bytes)` | writes that many request bytes, then loses the connection; `0` writes nothing, and any more counts the whole head as written, as httpx2's trace does |
| `Slow(seconds, then=Respond())` | waits (honoring the read timeout), then takes `then` |
| `RefusedStream()` | resets the HTTP/2 stream with `REFUSED_STREAM`, exactly as httpx2 raises it |

<!-- docs-test: run -->
```python
from headgate.testing import ConnectFail, DropMidBody, FaultyTransport, Respond

faults = FaultyTransport(schedule=[ConnectFail(), DropMidBody(after_bytes=200), Respond(201, json={"id": "o-1"})])
```

With that schedule, a POST without an idempotency key is re-sent after the `ConnectFail` (nothing was written) but not
after the `DropMidBody` (bytes were written): the call raises `TransportError` and `faults.calls` shows two sends.
`faults.calls`, like `fake.calls`, is a tuple read afresh each time, of one `FaultCall` per send: its step, what it
sent and what it got. A send past the end of the schedule raises `AssertionError`.

## Recorded cassettes

vcrpy 8.3 and later record httpx2's own transports, so they see what headgate's default network transport sends, once
per attempt, below the whole stack. `CassetteFilter` keeps credentials out of the cassette:

<!-- docs-test: run -->
```python
import pytest

from headgate import HttpOptions
from headgate.testing import CassetteFilter

CASSETTES = CassetteFilter(auths=(partner_auth,), headers=("x-partner-signature",))


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, object]:
    return CASSETTES.vcr_config()


middleware = (CASSETTES,)  # the client under test's middleware=
```

It filters `Authorization`, `Proxy-Authorization`, `Cookie`, every header and parameter the `auths=` declare (each an
auth or a middleware; anything that declares nothing raises `TypeError`) and the names given (each a collection, such as
`("x-partner-signature",)`: a bare `str` raises `TypeError`, since it would be read one letter at a time), redacts
secret query values and userinfo before a request is recorded or matched, and drops `Set-Cookie` from recorded
responses. Installed as middleware, it marks a `CachedTokenAuth` mint's hops so they are not recorded: a token response
is a credential. When replaying, seed the auth with a token (`auth.seed(Minted("test-token", 3600))`). A custom
transport (`FakeTransport`, the curl transport) is not patched by vcrpy, so it is neither recorded nor replayed.
`RecordedRequest` is the protocol of the vcrpy request `before_record_request` reads, for a hook of your own that calls
it first.

## Switching the policy off

A test that asserts on the first failure, as the fake network sends it, wants no retries, no breaker and no pacing.
`headgate.testing.policy_disabled()` builds the clients constructed in its block with retries off (whatever a call or
route asks for), no breaker (the client never reads a registry, so it cannot be refused by one another client opened),
and a budget that paces nothing, also for its routes' `rate=` and a shared `RateBudget` passed in.

<!-- docs-test: run -->
```python
from collections.abc import Iterator

import pytest

from headgate.testing import policy_disabled


@pytest.fixture(autouse=True)
def _no_call_policy() -> Iterator[None]:
    with policy_disabled():
        yield
```

Prefer `instant_retries()` when the retry behavior is part of what you test: it keeps the retries and makes them
instant.
