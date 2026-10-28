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
- the retry layer starts no retry, hedge or wait with less than `RetryPolicy.min_attempt_time` (0.05 s) left, and
  raises the last failure with the note `deadline exhausted` instead (`min_attempt_time` never holds back a request's
  first attempt);
- a rate-budget pause that would outlast the deadline is not waited for. On the first attempt the call raises
  `RateLimitError` at once, with `retryable=True` and `retry_after` the pause; on a retry, the call ends with the
  failure it already has, noted `deadline exhausted`;
- a turn in the rate budget that comes at or after the deadline is not waited for. On the first attempt the call
  raises `ClientTimeoutError` at once, naming it as never sent, with `attempts=0`, `retryable=True` and `retry_after`
  the wait it was refused. On a retry, the call ends with the failure it already has, noted `deadline exhausted`, and a
  hedge is simply not sent;
- after a rate-budget wait the send's timeouts are cut again, to what the wait left.

A deadline that passes before the request is sent (waiting for an in-flight slot, say) says so too:
`GET item/3: deadline exceeded after 3s, before attempt 1 was sent`, with `attempts` counting only the sends made.

For [`stream()`](streaming.md) the deadline bounds opening the stream (every attempt, backoff and the head checks), not
reading it: the caller's pace of iteration decides how long the body takes. Wrap the block in `asyncio.timeout` to
bound the whole body.

## Timeouts

`Timeouts(connect=10.0, read=30.0, write=10.0, pool=15.0)` are the defaults, per phase, in seconds: obtaining a pooled
connection (`pool`), connecting, writing the request, and waiting for each read. A `Timeouts` sets only the phases it
names, so `HttpOptions(timeout=Timeouts(read=60.0))` keeps the other three defaults.

The layers stack the same way: a view made with `with_options(timeout=...)` replaces the phases it sets over the
client's, and a call's `timeout=` the phases it sets over the view's. Every value must be positive.

A timeout must fit the deadline it runs under, or it could never take effect: each attempt's timeouts are cut to the
time left. So a timeout longer than that deadline is refused with `ValueError` where it is set: on the client (against
the policy's deadline), on a view, or on a call. Only the phases a `Timeouts` sets are checked. Shortening the deadline
instead is always allowed; cutting the timeouts is what a short deadline is for. To give a slow endpoint a long read
timeout, raise the deadline with it:

```python
reports = client.with_options(timeout=Timeouts(read=300.0), deadline=600.0)
```

## Choosing values

- Keep `read` well below the deadline, or a single slow response takes the whole call and leaves no time to retry it.
- A long export or report endpoint wants a `Route` with a longer deadline rather than a client-wide one.
- `deadline=None` exists for calls that legitimately run unbounded (a long poll); prefer a generous number.
