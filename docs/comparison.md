---
title: Compared with others
---

# Compared with others

headgate is a Swiss Army knife for HTTP API clients. Most libraries do one job well: send requests, retry, rate-limit,
cache, break circuits, mock. headgate does all of those jobs in one base class, and makes them agree with each other.
This page shows what that buys you, library by library, and when another tool is the better pick.

<div class="hg-legend" markdown>
<span>:lucide-circle-check:{ .hg-yes } built in</span>
<span>:lucide-puzzle:{ .hg-addon } with another library, or code you write</span>
<span>:lucide-minus:{ .hg-no } not available</span>
</div>

## HTTP clients

requests, niquests, httpx and aiohttp send HTTP requests, and do it well. headgate runs on httpx2 (pydantic's fork of
httpx) and adds everything a client for someone else's API needs on top. It can also send through
[curl_cffi](configuration.md#the-curl-transport) when you need libcurl.

<div class="hg-matrix hg-compare" markdown>

| | requests | niquests | httpx | aiohttp | headgate |
| --- | --- | --- | --- | --- | --- |
| Async | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
| HTTP/2 | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
| Retries with backoff | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| Waits for `Retry-After` | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| One deadline over every retry | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| Rate limiting that obeys the server | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| Circuit breaker | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| Caching | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| Pagination | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| Server-sent events | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| RFC 9457 problem details parsed | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
| Every credential kept off other hosts | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| SSRF guard with DNS pinning | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| Response size cap | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| Ignores proxy variables and `.netrc` by default | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
| Secrets redacted from error messages | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
| Test fakes | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
| Sync API | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } |

</div>

When a redirect leaves the origin, requests and httpx drop the `Authorization` header, and aiohttp drops `Cookie` and
`Proxy-Authorization` too. headgate drops those and every other header and query parameter its auth declares, such as
an `X-API-Key`, on every hop, next-page links included: see [security](security.md).

## The stack headgate replaces

| Job | What people usually add | In headgate |
| --- | --- | --- |
| :lucide-rotate-cw: Retries | tenacity, stamina, backoff, httpx-retries, aiohttp-retry | [`RetryPolicy`](retries.md) |
| :lucide-gauge: Rate limits | aiolimiter, pyrate-limiter, limits | [`RateBudget`](rate-budgets.md) |
| :lucide-database-zap: Caching | hishel, requests-cache, aiohttp-client-cache, CacheControl | [`HttpCache`](caching.md), on hishel |
| :lucide-zap-off: Circuit breaking | pybreaker, aiobreaker, purgatory, circuitbreaker | [`BreakerPolicy`](breaker.md) |
| :lucide-radio: Server-sent events | httpx-sse, aiohttp-sse-client | [`stream().aiter_events()`](streaming.md#server-sent-events) |
| :lucide-list-ordered: Pagination | a loop you write per endpoint | [`paginate()`](pagination.md) |
| :lucide-flask-conical: Test doubles | respx, pytest-httpx, aioresponses, responses | [`headgate.testing`](testing.md); vcrpy cassettes work too |
| :lucide-activity: Telemetry | OpenTelemetry instrumentation packages | [typed events and an OpenTelemetry observer](observability.md) |

Fewer dependencies is the small win. The big one is that the layers know about each other, which separate libraries
cannot:

- **A cache hit is never retried, and spends no rate turn**, because the cache sits below the retry layer and above
  the rate budget.
- **The retry layer and the rate budget read `Retry-After` the same way**, so the retry's wait and the budget's pause
  end together instead of stacking.
- **One deadline bounds everything**: every attempt, every backoff, every rate-budget wait and the body read.
- **A breaker counts only the host's own failures**: a 404 your code asked for, or a call you cancelled, does not open
  it.

## Library by library

=== "Retries"

    <div class="hg-matrix hg-compare" markdown>

    | | tenacity | stamina | backoff | urllib3 `Retry` | headgate |
    | --- | --- | --- | --- | --- | --- |
    | Retries any code, not just HTTP | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } |
    | Async | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | Knows which HTTP requests are safe to re-send | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
    | Waits for `Retry-After` | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
    | Total time limit | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | Retry budget shared across calls | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | Idempotency keys for safe POST retries | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | Hedged requests | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | Instant retries in tests | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |

    </div>

    **Pick stamina or tenacity** to retry something that is not an HTTP call, such as a database query. stamina's
    `on=` hook (25.2 and later) and tenacity's `wait_exception` can honor `Retry-After` with a little code. backoff's
    repository is archived, and it has not been released since 2022.

=== "Rate limits"

    <div class="hg-matrix hg-compare" markdown>

    | | aiolimiter | pyrate-limiter | headgate |
    | --- | --- | --- | --- |
    | Async | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
    | Several rates at once | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
    | Shared across processes (Redis, SQLite) | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } |
    | Pauses when the server says 429 or 503 | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | Reads `RateLimit` and `X-RateLimit-*` headers | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | Slows down on pushback (adaptive) | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | Separate lanes per path | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
    | Cap on requests in flight | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |

    </div>

    **Pick pyrate-limiter** when many processes or machines must share one quota.

=== "Caching"

    <div class="hg-matrix hg-compare" markdown>

    | | hishel | requests-cache | aiohttp-client-cache | headgate |
    | --- | --- | --- | --- | --- |
    | Async | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
    | Follows `Cache-Control` (RFC 9111) | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } |
    | Your own rules for APIs that send no cache headers | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
    | Entries kept apart per credential | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
    | Stores only answers your code accepted | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |

    </div>

    headgate's cache **is** hishel, configured with your rules and wrapped so it is safe to share between
    credentials. **Pick hishel directly** when you want the provider's `Cache-Control` to decide; it supports httpx2
    too.

=== "Circuit breakers"

    <div class="hg-matrix hg-compare" markdown>

    | | pybreaker | aiobreaker | purgatory | headgate |
    | --- | --- | --- | --- | --- |
    | Async | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
    | One breaker per host, automatically | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
    | Counts only the host's own failures | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
    | Retries stop while it is open | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-puzzle:{ .hg-addon } | :lucide-circle-check:{ .hg-yes } |
    | State shared across processes (Redis) | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } |

    </div>

    **Pick pybreaker or purgatory** to guard something that is not HTTP, or to share breaker state across machines.

=== "Testing"

    <div class="hg-matrix hg-compare" markdown>

    | | respx | pytest-httpx | aioresponses | vcrpy | headgate.testing |
    | --- | --- | --- | --- | --- | --- |
    | Canned responses | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } |
    | Record and replay real traffic | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } | :lucide-puzzle:{ .hg-addon } |
    | Failure schedules (fail twice, then succeed) | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-circle-check:{ .hg-yes } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | A fake clock, so retries and rate waits are instant | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |
    | Breaker state isolated per test | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-minus:{ .hg-no } | :lucide-circle-check:{ .hg-yes } |

    </div>

    respx and pytest-httpx patch httpx, not httpx2, so they do not see a headgate client's requests.
    `headgate.testing` is built for it, and vcrpy 8.3 and later record httpx2; headgate ships
    [filters](testing.md) that keep credentials out of the cassettes.

=== "Client frameworks"

    | | What it is | How headgate differs |
    | --- | --- | --- |
    | **uplink** | declare endpoints with decorators on an interface | headgate endpoints are ordinary methods, and resilience is built in rather than added per decorator |
    | **openapi-python-client** | generates a typed client from an OpenAPI document | use it when you have a spec; headgate is for APIs without one, or where you want control |
    | **Stainless SDKs** (openai, anthropic) | generated SDKs with retries and timeouts built in | headgate brings that quality to a client you write yourself, and understands the same `retry-after-ms` and `x-should-retry` headers |

## When to use something else

- **Retrying code that is not an HTTP call** (a database query, a file operation): use stamina, or tenacity when you
  need its full flexibility.
- **Python before 3.14**: headgate needs 3.14 for its template-string paths. Use httpx with stamina, and add rate
  limiting and caching yourself.
- **Upstream httpx**: headgate runs on httpx2 and refuses httpx objects, to catch the mix-up early. If your code has to
  stay on httpx, headgate is not for you yet.
- **A synchronous codebase**: headgate is async only. requests or httpx's sync client is the simpler choice.
- **One or two calls in a script**: a plain httpx2 call is fine. headgate pays off when a client has several endpoints,
  several consumers, or has to survive a provider's bad day.
- **A quota shared across machines**: headgate's rate budget and breakers live in one process. Use pyrate-limiter or
  purgatory with Redis for that.

## Why httpx2

headgate is built and tested on [httpx2](https://pypi.org/project/httpx2/), pydantic's fork of httpx, and on nothing
else. Its API is httpx's, so moving a client over is mostly an import change: `import httpx2` where you had
`import httpx`, and the same for the exceptions you catch. headgate refuses an `httpx` object anywhere it takes an httpx2
one, with a `TypeError` naming the mistake, rather than failing later in a confusing way.
