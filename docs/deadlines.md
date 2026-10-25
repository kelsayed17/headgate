# Deadlines and timeouts

A timeout bounds one phase of one send. A caller cares about something else: how long the **call** may take, retries,
backoff, rate-budget waits, a token mint and the body read included. headgate gives every call a deadline, and fits
each send's timeouts inside it.

## The deadline

`TransportPolicy(deadline=60.0)` is the default: every call ends within 60 seconds, or raises `ClientTimeoutError`
(`GET series/GDP: deadline exceeded after 60s`). It is set, from the most specific to the least:

1. the call's `deadline=` (seconds, or `None` for no deadline);
2. a `with_options(deadline=...)` view's;
3. the first matching `Route(fragment, deadline=...)` in `TransportPolicy(routes=...)`;
4. the policy's `deadline`.

```python
from headgate import Route, TransportPolicy

policy = TransportPolicy(deadline=20.0, routes=(Route("/reports", deadline=300.0),))
```

```python
report = await client.get(t"reports/{report_id}", expect=REPORT, deadline=600.0)
```

The deadline is measured on the client's clock from the moment the call is prepared, and everything inside it counts:
waiting for the rate budget, every attempt and backoff, a token mint's hops, redirects, and reading the body. Layers
below act on it as well as the outer timer:

- each attempt's connect, read, write and pool timeouts are cut to the time left, so a send cannot outlast the call;
- the retry layer starts no attempt or wait with less than `RetryPolicy.min_attempt_time` (0.05 s) left, and raises
  the last failure with the note `deadline exhausted` instead;
- a rate-budget pause that would outlast the deadline raises `RateLimitError` at once rather than waiting;
- after a rate-budget wait the send's timeouts are cut again, to what the wait left.

For [`stream()`](streaming.md) the deadline bounds opening the stream (every attempt, backoff and the head checks), not
reading it: the caller's pace of iteration decides how long the body takes. Wrap the block in `asyncio.timeout` to
bound the whole body.

## Timeouts

`HttpOptions(timeout=Timeouts(connect=10.0, read=30.0, write=10.0, pool=15.0))` are the defaults, per phase, in
seconds: obtaining a pooled connection (`pool`), connecting, writing the request, and waiting for each read. A bare
number replaces the read timeout only: `HttpOptions(timeout=60.0)` keeps the other three defaults.

A call overrides them with `timeout=`: a `Timeouts` replaces all four, a number the read timeout alone. A view made with
`with_options(timeout=...)` does the same for its calls. Every value must be positive.

## Choosing values

- Keep `read` below the deadline, or a single slow response takes the whole call and leaves no time to retry it.
- A long export or report endpoint wants a `Route` with a longer deadline rather than a client-wide one.
- `deadline=None` exists for calls that legitimately run unbounded (a long poll); prefer a generous number.
