# Circuit breaker

When an origin is down, every call to it spends a connect timeout, a retry sequence and the caller's patience, and adds
load to whatever is trying to recover. A circuit breaker notices the failures, refuses calls to that origin at once for
a while, then lets one call through to test whether it has recovered.

## Behaviour

Each origin (`scheme://host:port`) has a breaker with three states:

- **closed**: calls pass. `failure_threshold` (default 5) **consecutive** origin failures open it; any success resets
  the count, so an origin failing half its calls never trips.
- **open**: calls are refused without being sent, as `CircuitOpenError` (a `TransportError`) with `retry_after` set to
  the cooldown left. After `recovery_timeout` seconds (default 60) the next call becomes the probe.
- **half-open**: exactly one call, the probe, is let through; its outcome closes or re-opens the breaker. Other calls
  meanwhile are refused with `retry_after=None`, since the probe's outcome decides. The probe keeps its own retries, so
  its whole retry sequence is the one recovery test.

The breaker spans the send **and the body read**, so a connection that dies mid-body is a failure of the fetch. A
retry sequence counts once, however many attempts it made, and the retry layer stops re-sending as soon as the breaker
opens. A cached response is not sent, but admission is decided before the cache is asked, so an open breaker refuses a
call the cache could have answered.

## What counts

Only what the origin itself showed:

| Counts as a failure | Records nothing |
| --- | --- |
| a 5xx that survived the retries | a 429: the origin is up and asked us to slow down, which the rate budget handles |
| a network failure or timeout, in the send or the body read | our own side's failures: an unencodable header, an unsupported scheme, an invalid URL, our pool running out of connections |
| | a failure on a hop an auth flow made to mint a credential (that is the token host's failure) |
| | a cache hit, a stored 5xx, or an outcome shared from another identical request's fetch |
| | a call cancelled (by its caller, or by the deadline's own timer), a body over the size cap, a refused SSRF destination |

Every other response is a success: a 4xx says the origin is answering.

## Sharing and keying

Breakers live in a `BreakerRegistry`, by default the process-wide `headgate.breaker.DEFAULT_REGISTRY`. Every client
whose `BreakerPolicy` names the same registry and the same thresholds shares an origin's breaker, so two clients of one
upstream cannot each spend a full quota of failures against it. A client that asks for other thresholds gets a breaker
of its own for that origin, with exactly the thresholds it asked for, rather than whichever thresholds the first client
to call happened to set.

```python
from headgate import BreakerPolicy, BreakerRegistry, TransportPolicy

critical = BreakerRegistry(max_hosts=256)
policy = TransportPolicy(breaker=BreakerPolicy(failure_threshold=3, recovery_timeout=30.0, registry=critical))
no_breaker = TransportPolicy(breaker=None)
```

A registry is thread-safe (clients on different event loops and threads can share it) and bounded: past `max_hosts`
breakers (default 1024) it drops the least recently used closed one, or, when every breaker is open or half-open, the
least recently used of all. `TransportPolicy(breaker=None)` turns the breaker off; such a client never reads a registry,
so it is not refused by a breaker another client opened.

## Watching it

Every transition a call causes is a `BreakerChanged` event (origin, previous and new state, failure count, and the
call). `headgate.health.breaker_states(registry=DEFAULT_REGISTRY)` returns a snapshot per origin, with `refusing`
alongside `state`: an open breaker whose cooldown has elapsed still reads open until a call takes the probe, so
`refusing` is what says whether a call made now would be turned away. Where clients with different thresholds keep
several breakers for one origin, the snapshot reports the most degraded. See [observability](observability.md).

In tests, `headgate.testing.isolated_breakers()` gives the clients built in its block a fresh registry, so no breaker
state crosses between tests. See [testing](testing.md).
