---
hide:
  - toc
---

# Compared with others

headgate is a base for **HTTP API clients**: it owns the whole call, from building the request to reading the body,
and handles the concerns every client shares. The libraries people usually reach for each solve one of those concerns,
for any code. This page says what that difference buys, and when to use them instead.

## At a glance

<div class="hg-compare" markdown>

| | tenacity | stamina | a hand-rolled httpx client | headgate |
| --- | --- | --- | --- | --- |
| Scope | retry any callable | retry any callable (an opinionated tenacity wrapper) | what you write | HTTP API clients |
| What is retried | the whole decorated call | the whole decorated call | what you write | only the send: below auth and the cache, so a cache hit is never retried and each re-send spends its own rate turn |
| Which requests are safe to re-send | your predicate | your `on=` | what you write | idempotent methods; a POST or PATCH only when it provably never left, or under an idempotency key stamped once per call |
| `Retry-After` | write it yourself | write it yourself | write it yourself | waited exactly, up to a cap; past it the call fails at once with `retry_after` |
| Total deadline | `stop_after_delay` | `timeout=` | write it yourself | one deadline over every attempt, backoff, rate-budget wait and the body read |
| Retry budget across calls | no | no | write it yourself | yes, so retries cannot multiply an outage |
| Rate limiting | no | no | add aiolimiter or similar | a shared budget that reads `RateLimit` and `X-RateLimit-*` headers and backs off on pushback |
| Circuit breaker | no | no | add a breaker library | per origin, thread-safe, charged only for the host's own failures |
| Caching | no | no | add hishel | hishel, partitioned by credential, never storing credentials, storing only what the call accepted |
| Credential safety | n/a | n/a | write it yourself | credentials pinned to their origin, an https-to-http redirect refused, an optional SSRF guard |
| Observability | `before_sleep` and other hooks | instrumentation built in | write it yourself | typed events and an OpenTelemetry observer |
| Test support | none built in | `set_testing()` | patch what you must | `headgate.testing`: a fake network, instant retries, failure schedules, a test clock |

</div>

## When to use something else

- **Retrying code that is not an HTTP call** (a database query, a file operation): use stamina, or tenacity when you
  need its full flexibility. headgate's retry lives in its transport stack and retries only HTTP sends.
- **Python before 3.14**: headgate needs 3.14 for its template-string paths. Use httpx with stamina or tenacity, and
  add rate limiting and caching yourself.
- **Upstream httpx**: headgate runs on httpx2 and refuses httpx objects, to catch the mix-up early. If your code has to
  stay on httpx, headgate is not for you yet.
- **A synchronous codebase**: headgate is async only.
- **One or two calls in a script**: a plain httpx2 call is fine. headgate pays off when a client has several endpoints,
  several consumers, or has to survive a provider's bad day.

## Why httpx2

headgate is built and tested on [httpx2](https://pypi.org/project/httpx2/), pydantic's fork of httpx, and on nothing
else. Its API is httpx's, so moving a client over is mostly an import change: `import httpx2` where you had
`import httpx`, and the same for the exceptions you catch. headgate refuses an `httpx` object anywhere it takes an httpx2
one, with a `TypeError` naming the mistake, rather than failing later in a confusing way.
