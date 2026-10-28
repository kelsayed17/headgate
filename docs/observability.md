# Observability

headgate writes no log records. A client reports what it does as **typed events** to the observers you install, its
errors carry what a diagnosis needs (see [errors](errors.md)), and three counters of degraded state are readable on
demand. What to record, where, and at what level is the application's decision.

## Events

An observer is any object with a `notify(event)` method, installed with `HttpOptions(observers=(...))`:

```python
from headgate import HttpOptions
from headgate.events import CallFinished, Event, RateLimited


class Metrics:
    def notify(self, event: Event) -> None:
        match event:
            case CallFinished(call=call, status_code=status, error=error, elapsed=elapsed):
                record_call(call.method, call.route or call.url, status, error, elapsed)
            case RateLimited(delay=delay, reason=reason):
                record_wait(reason, delay)
            case _:
                pass


http = HttpOptions(observers=(Metrics(),))
```

| Event | When | Carries |
| --- | --- | --- |
| `CallStarted` | a call's request is built | `call` |
| `AttemptFinished` | each send ends: a response head arrived, or it raised | `attempt`, `status_code` or `error`, `elapsed` |
| `RetryScheduled` | the retry layer will send again | `attempt`, `delay`, `reason` (`"503"`, `"ConnectError"`, `"hedge"`) |
| `RateLimited` | the rate budget held the call, or its response paused the budget | `delay`, `reason`: `paced`, `throttled`, `paused`, `pushback` |
| `BreakerChanged` | an origin's breaker changed state | `origin`, `previous`, `state`, `failures`, `call` |
| `CacheResult` | the cache answered | `outcome`: `hit`, `miss`, `shared`, `degraded` |
| `Deprecated` | the response announced deprecation or a sunset | `deprecated`, `deprecated_at`, `sunset_at`, `link`, `sunset_link` |
| `CallFinished` | a call ends, either way | `status_code`, `error` (the exception type's name; not that of a non-`ClientError` a `stream()` block raised itself), `attempts`, `elapsed` |

Every call event carries `call`, a `CallInfo`: the `method`, the `url` as given (sanitized), the `route` (the t-string
template, `"series/{series_id}"`, or `None` for a `str` URL), the absolute `full_url` (sanitized), and a process-unique
`call_id` that groups the events of one call among many concurrent ones. Durations are seconds on the client's clock.

Events carry **sanitized data only**: no header, no body, no credential. They are frozen dataclasses, so an observer can
queue them for another thread.

Observers run inline, in the order given, on the task making the call; hand slow work to a queue. An exception an
observer raises never reaches the call: it is swallowed and counted per observer class in
`headgate.health.observer_failures()`. A client without observers builds no events at all.

## OpenTelemetry

`pip install 'headgate[otel]'` (it needs only `opentelemetry-api`; bring your own SDK):

```python
from headgate import HttpOptions
from headgate.otel import OpenTelemetryObserver

otel = OpenTelemetryObserver()  # the global tracer and meter providers by default
http = otel.install(HttpOptions(user_agent="shop-sync/1.4"))
```

`install` adds it as an observer and as the outermost middleware, and every call then gets:

- **one CLIENT span per logical call**, named `GET` or `GET series/{series_id}`, covering every attempt, backoff wait,
  token mint and the body read, with the stable HTTP semantic-convention attributes: `http.request.method`,
  `server.address`, `server.port`, `url.full` (redacted), `url.template`, `http.response.status_code`,
  `http.request.resend_count` and `error.type` (the status for a 4xx or 5xx, else the `ClientError` class name). A
  failed call's status is `ERROR`; a cancelled call's is left unset;
- **the `http.client.request.duration` histogram**, with the same attributes less `url.full`, `url.template` and
  `http.request.resend_count`;
- **W3C Trace Context on every attempt**: `traceparent` (and `tracestate`) is written onto each send, re-sends included,
  and the call's span is current while an attempt is in flight. Baggage is not sent unless you pass a propagator that
  sends it.

It works with any SDK `TracerProvider`, with Logfire, and with Sentry's OpenTelemetry integration. For a span per
physical send as well, add `opentelemetry-instrumentation-httpx`'s `HTTPX2ClientInstrumentor().instrument()`: it
patches httpx2's network transport, below headgate's stack, so its spans nest under headgate's. Both then record
`http.client.request.duration`, so give one a no-op meter provider (`OpenTelemetryObserver(meter_provider=...)`).

## Request ids

Every `ClientError` and every `Stream` has `request_id`, read from the first of `HttpOptions.request_id_headers` the
response carries: `x-request-id`, `request-id`, `x-amzn-requestid`, `x-ms-request-id` and `cf-ray` by default. Put a
provider's own header first if it uses another.

## Deprecation and sunset

A provider retiring an endpoint says so on its responses. When a response carries a valid `Deprecation` header (RFC
9745's `@<epoch>` date, or the older `true` or HTTP-date forms) or a `Sunset` date (RFC 8594), the call emits a
`Deprecated` event with the parsed dates and the `rel="deprecation"` and `rel="sunset"` links, resolved and redacted.
An observer that alerts on it finds every endpoint a client depends on that is going away, before it does.

## User-Agent

Every request carries `User-Agent: headgate/<version> httpx2/<version> python/<version>`.
`HttpOptions(user_agent="shop-sync/1.4")` puts your product token first, which is what a provider's support team sees.
A `User-Agent` in `HttpOptions(headers=...)` replaces it entirely.

## Health snapshots

`headgate.health` reads the process-wide state that says something is degraded, as fresh copies a metrics callback can
poll:

- `breaker_states(registry=...)`: a `BreakerSnapshot(state, refusing)` per origin, of the shared registry by default;
- `cache_degradations()`: per host, how many requests found the cache broken and went to the network;
- `observer_failures()`: per observer class, how many events its `notify` raised on.
