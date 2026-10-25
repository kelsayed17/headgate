# Retries

A retry that repeats a write the server already processed charges a card twice; a retry storm turns a brief outage into
a long one. headgate's retry layer re-sends a request only when that is **safe** (the request was not processed, or
repeating it is harmless) and **worthwhile** (the failure is transient, the budget allows it, the deadline leaves time,
and the origin's breaker is not refusing).

Configure it once with `TransportPolicy(retry=RetryPolicy(...))`; override it for a call with `retry=RetryPolicy(...)`,
turn it off with `retry=False`, or set it per route with `Route(fragment, retry=...)`.

```python
from headgate import Backoff, Hedge, RetryBudget, RetryPolicy, TransportPolicy

policy = TransportPolicy(
    retry=RetryPolicy(
        attempts=4,
        backoff=Backoff(factor=0.25, max_wait=8.0),
        budget=RetryBudget(ratio=0.1),
        hedge=Hedge(after=0.8),
    )
)
```

| `RetryPolicy` field | Default | Meaning |
| --- | --- | --- |
| `attempts` | `3` | sends per request, the first included (`1` never retries); hedges count |
| `methods` | `GET HEAD OPTIONS PUT DELETE` | the methods treated as idempotent |
| `statuses` | `408 425 429 500 502 503 504` | the statuses that are retryable failures |
| `backoff` | `Backoff(factor=0.5, max_wait=5.0)` | the wait when the response names none |
| `max_retry_after` | `30.0` | the longest `Retry-After` that is waited |
| `budget` | `RetryBudget(ratio=0.2, min_per_second=10, window=10)` | the share of traffic retries may add, or `None` |
| `respect_should_retry` | `True` | follow a server's `x-should-retry: true` or `false` |
| `hedge` | `None` | an opt-in `Hedge` for slow idempotent requests |
| `min_attempt_time` | `0.05` | the least time left before the deadline that an attempt or a wait starts with |

`NO_RETRY` is `RetryPolicy(attempts=1)`, what `retry=False` installs.

## What is re-sent

Each attempt's result is classified, in this order:

1. **An answer** (any status not in `statuses`, unless the server says `x-should-retry: true`) is returned.
2. **A local mistake** (`LocalProtocolError`, such as a header value that cannot be encoded; `UnsupportedProtocol`;
   `InvalidURL`) is never re-sent: the same request would fail the same way.
3. **The server's word** decides when it sends `x-should-retry: true` or `false`.
4. **Not processed** is retryable for every method: a `425 Too Early` (RFC 8470), and an HTTP/2 stream the server
   reset with `REFUSED_STREAM` (RFC 9113). A GOAWAY covering streams the server never started is retried inside the
   connection pool, below this layer.
5. **An idempotent method** (`methods`) or **a request carrying an idempotency key** is retryable.
6. **Anything else** (a POST or PATCH without a key) is retryable only when **nothing of it was written**. That is a
   recorded fact, not a guess: a layer directly above the network tracks, per attempt, whether a connection was
   obtained and how many request bytes went out. A `ConnectError`, `ConnectTimeout` or `PoolTimeout` before the write
   began is safe; a `ReadTimeout` after the body was sent is not. A custom transport that reports no progress is judged
   by the exception's type instead.

A request whose body is a one-shot stream (`content=` an async iterable) is never sent twice; the error notes why. Give
`content=` a factory instead to keep it retryable (see [streaming](streaming.md#request-bodies)).

The retry policy applies to each request the client sends: a followed redirect's hop, and a token-mint hop of a
[`CachedTokenAuth`](auth.md#cachedtokenauth), each run their own sequence of attempts.

## Backoff

Between attempts the layer waits `factor · 2^n · jitter` before retry *n* (full jitter: `jitter` is a uniform draw in
`[0, 1)`), capped at `max_wait`. With the defaults that is up to 1 s before the first retry and up to 2 s before the
second. The draw comes from the operating system's generator rather than `random`, so an application that seeds
`random` does not synchronise its processes' retries. A failed response is read (a bounded head of its body) and
closed before the wait, so no connection is held through it.

## Retry-After

A 429's or 503's `retry-after-ms` or `Retry-After` (whole or fractional seconds, or an HTTP-date) replaces the backoff
and is waited **exactly**, not capped by `max_wait`, when it is at most `max_retry_after`. A longer one is not waited:
the call stops and raises `RateLimitError` (429) or `UpstreamStatusError` with `retry_after` set and `retryable=True`,
so the caller can reschedule. The [rate budget](rate-budgets.md#server-feedback) reads the same header the same way,
so the retry's wait and the budget's pause end together.

## The retry budget

Retries may add at most `ratio` of the first attempts made over the last `window` seconds, plus a floor of
`min_per_second · window` so a quiet client can still retry: with the defaults, 20% plus 10 a second. Hedges spend the
same budget. A client keeps one account per distinct `RetryBudget` value, shared by every call it makes and by its
`with_options` views, so once traffic is above the floor a failing upstream sees about 1.2 times its normal load from
a client, not three times. A call that finds the budget
spent raises its last failure with the note `retry budget exhausted`. `budget=None` removes the cap.

## Deadlines and the breaker

Each attempt's connect, read, write and pool timeouts are cut to the time left before the call's
[deadline](deadlines.md). No attempt, and no wait before one, starts with less than `min_attempt_time` left: the call
raises its last failure with the note `deadline exhausted` instead of sleeping past the deadline.

The retry layer checks the origin's [circuit breaker](breaker.md) before every retry and after every wait, and stops
with the note `circuit opened` once it refuses. A whole retry sequence counts once toward the breaker.

## Hedging

A hedge sends a second copy of a slow idempotent request instead of waiting for it to time out. With
`RetryPolicy(hedge=Hedge(after=0.5, max_extra=1))`, a request that has not answered after 0.5 s is sent again beside
the first; the first answer wins, and the other send is cancelled and its response closed. A retryable failure from
one send leaves the others running. Hedges count as attempts, spend the retry budget, respect the deadline and the
breaker, and appear in events (`RetryScheduled` with reason `hedge`) and in the history (`attempt 2/3 (hedge): ...`).

Only `methods` are hedged, and only with a replayable body. A policy whose `methods` include POST or PATCH refuses a
hedge when it is constructed.

## Idempotency keys

A POST is safe to retry once the server can recognise a re-send. `TransportPolicy(idempotency=IdempotencyKeys())`
stamps a fresh key on every POST and PATCH, under `Idempotency-Key` by default (`IdempotencyKeys(header=...,
factory=...)` changes either). A call can pass its own with `idempotency_key=`, and a key set in `headers=` is kept.
The key is stamped once per call, before the retry layer, so every attempt carries the same one, and a keyed request is
retried like an idempotent one.

## What `retryable` means

`ClientError.retryable` is the retry policy's own verdict on the outcome that ended the call, by the rules above: it is
`True` exactly when the call's `RetryPolicy` would re-send after it. So a surviving 503 on a GET is retryable, while a
501 or 505 (outside `statuses`), a 503 the server marked `x-should-retry: false`, and a 503 on a POST without an
idempotency key are not. A network failure while reading the body is judged as one in the send would be: retryable for
an idempotent or keyed request, not for a POST that was written in full. The flag ignores what stopped the sequence
(spent attempts, the budget, the deadline), since it answers whether sending again later could help.

Two errors set it otherwise: a `RateLimitError` from a [rate budget](rate-budgets.md) paused past the deadline is
retryable after its `retry_after`, and a token mint on a client of its own, which no retry layer judged, is retryable for
any network failure that is not a local mistake.

## Attempt history

The error a call raises carries `attempts`, `retryable`, `retry_after`, and a note per attempt:

```text
attempt 1/3: 503 Service Unavailable (Retry-After 2.0s) after 0.41s
attempt 2/3: ReadTimeout after 30.0s
attempt 3/3: 502 Bad Gateway after 0.12s
```

Notes name statuses and exception types only, never a URL, a header or a message, so they carry no credential.
`str(error)` stays one line; the notes print under the traceback.

## Testing retries

`headgate.testing.FaultyTransport` plays a schedule of faults and records what each attempt sent. See
[testing](testing.md#fault-injection).
