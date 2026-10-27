# Testing a client

`headgate.testing` tests a client through its real stack (retries, the rate budget, the breaker, the error mapping)
with nothing private patched. Two seams make that possible:

- **the network** is `HttpOptions(transport=...)`. Have the client accept a transport and pass it through, as the
  [examples](https://github.com/kelsayed17/headgate/tree/main/examples) do: `CatalogClient(api_key, transport=fake)`;
- **time and breaker state** are read when a client is **constructed**, from context variables the kit's context
  managers set. Build the client inside them; entering one around a client that already exists changes nothing.

```python
from collections.abc import Iterator

import pytest

from headgate.testing import FakeTransport, Respond, instant_retries, isolated_breakers


@pytest.fixture
def catalog() -> Iterator[CatalogClient]:
    with instant_retries(), isolated_breakers():
        yield CatalogClient("test-key", transport=FakeTransport().reply("GET", "/v1/series/GDP", Respond(json=GDP)))
```

## FakeTransport

A route table of canned replies and a log of every request:

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
- A reply is an `httpx2.Response` built with `content=`, `json=` or `text=` (copied for each request), a bare status, or
  a function of the request returning a response, sync or async. A function can raise an httpx2 error to simulate a
  network failure.
- `fake.calls` is every request sent, in order, body read; `fake.calls_to(method, path)` filters it; `fake.closed`
  says whether the client closed it.
- A request with no route raises `UnmatchedRequestError`, an `AssertionError` the client neither retries nor maps, naming
  the request and the routes there are.

## TestClock and instant retries

`TestClock()` is a monotonic clock that moves only when told: `clock.now`, `clock.advance(seconds)`, and an async
`clock.sleep` that records the wait in `clock.sleeps` and advances the clock instead of waiting. Clients built inside
`clock.install()` measure every duration on it (deadlines, events, rate pacing, and the cooldowns of a registry built by
`isolated_breakers()` in the same block) and wait through its sleep, so a test of a 60-second cooldown runs in no time.

`instant_retries()` keeps the real clock and makes every wait return at once, so a mocked failure is retried exactly as
production retries it, without the backoff.

## Isolated breakers

`isolated_breakers()` gives the clients built in its block a fresh `BreakerRegistry` (and yields it), so failures in one
test open no breaker in the next. Assert on it with `headgate.health.breaker_states(registry)`. A client configured
with a registry of its own keeps it.

## Fault injection

`FaultyTransport(schedule=[...], sleep=asyncio.sleep)` answers each send with the next step of its schedule, reports
send progress the way httpx2's own transport does (so the retry layer's "was anything written?" rule sees real facts),
and records each send as a `FaultCall`: the step, method, URL, headers, whether it connected, `bytes_sent`, the
response, whether that response was closed, and the error raised.

| Step | Does |
| --- | --- |
| `ConnectFail(timeout=False)` | raises `ConnectError` (`ConnectTimeout` with `timeout=True`); nothing sent |
| `PoolFull()` | raises `PoolTimeout`; nothing sent |
| `Respond(status_code=200, headers=..., json=..., content=..., retry_after=None)` | answers |
| `DropMidBody(after_bytes)` | writes that many request bytes, then loses the connection |
| `Slow(seconds, then=Respond())` | waits (honouring the read timeout), then takes `then` |
| `RefusedStream()` | resets the HTTP/2 stream with `REFUSED_STREAM`, exactly as httpx2 raises it |

```python
from headgate.testing import ConnectFail, DropMidBody, FaultyTransport, Respond

faults = FaultyTransport(schedule=[ConnectFail(), DropMidBody(after_bytes=200), Respond(201, json={"id": "o-1"})])
```

With that schedule, a POST without an idempotency key is re-sent after the `ConnectFail` (nothing was written) but not
after the `DropMidBody` (bytes were written): the call raises `TransportError` and `faults.calls` shows two sends. A
send past the end of the schedule raises `AssertionError`.

## Recorded cassettes

vcrpy 8.3 and later record httpx2's own transports, so they see what headgate's default network transport sends, once
per attempt, below the whole stack. `CassetteFilter` keeps credentials out of the cassette:

```python
import pytest

from headgate import HttpOptions
from headgate.testing import CassetteFilter

CASSETTES = CassetteFilter(auths=(partner_auth,), headers=("x-partner-signature",))


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, object]:
    return CASSETTES.vcr_config()


http = HttpOptions(middleware=(CASSETTES,))  # on the client under test
```

It filters `Authorization`, `Proxy-Authorization`, `Cookie`, every header and parameter the auths declare and the names
given, redacts secret query values and userinfo before a request is recorded or matched, and drops `Set-Cookie` from
recorded responses. Installed as middleware, it marks a `CachedTokenAuth` mint's hops so they are not recorded: a token
response is a credential. When replaying, seed the auth with a token (`auth.seed("test-token", 3600)`). A custom
transport (`FakeTransport`, the curl transport) is not patched by vcrpy, so it is neither recorded nor replayed.

## Switching the policy off

Some tests, and connectivity probes, want the first failure as it is: no retries, no breaker, no pacing.
`headgate.policy.policy_disabled()` builds the clients constructed in its block with retries off (whatever a call or
route asks for), no breaker (the client never reads a registry, so it cannot be refused by one another client opened),
and an effectively unlimited rate, also for its `rate_limits` rules and a shared `RateBudget` passed in.

```python
from collections.abc import Iterator

import pytest

from headgate.policy import policy_disabled


@pytest.fixture(autouse=True)
def _no_transport_policy() -> Iterator[None]:
    with policy_disabled():
        yield
```

Prefer `instant_retries()` when the retry behaviour is part of what you test: it keeps the retries and makes them
instant.
