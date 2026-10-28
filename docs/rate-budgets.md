# Rate budgets

Every request a client sends to the network spends from a **rate budget**: it waits for a free concurrency slot, if the
budget caps them, then for its turn at the configured rate and for any pause the server asked for. Waiting on the
client side is cheaper than collecting 429s, and a budget that listens to the server stops a fleet of callers from
hammering an API that has said "slow down".

```python
from headgate import AsyncBaseClient, Rate, RateBudget, RateRule, TransportPolicy

shared = RateBudget(Rate.per_minute(600), max_in_flight=8)  # one provider quota, two clients
search = AsyncBaseClient(base_url="https://api.example.com/search", rate=shared)
reports = AsyncBaseClient(
    base_url="https://api.example.com/reports",
    rate=shared,
    policy=TransportPolicy(rate_limits=(RateRule("/export", Rate.per_minute(6)),)),
)
```

## Rates and budgets

`Rate(requests, period)` is a value: `Rate.per_second(5)` and `Rate.per_minute(300)` are the same rate, and
`Rate.per_hour(5000)` states an hourly quota as a provider does. Up to `requests` may go out back to back, so state a
large hourly quota per minute or per second for a steadier pace. A client given
a `Rate` builds a budget of its own. A `RateBudget` is the budget as an object: pass one instance to several clients
(or rules) and they spend from it together, across tasks, event loops and threads, because its state is plain numbers
under a `threading.Lock` and nothing in it is bound to a loop.

`RateBudget(rate, *, max_in_flight=None, adaptive=Adaptive(), timing=..., wall_clock=time.time)`:

- **Pacing** is GCRA, the virtual-scheduling form of a leaky bucket. Up to `rate.requests` requests (at least one) go
  out back to back; after that they are `period / requests` seconds apart. Waiters are served in arrival order, and a
  waiter cancelled in the queue gives its turn back when nobody reserved after it. A call whose turn comes at or after
  its deadline does not queue: it gives the turn back and the attempt is never sent (see [deadlines](deadlines.md)).
- **`max_in_flight`** caps how many requests are open at once. A request takes its slot before its turn, so the
  requests that slots freed at one instant still go out at the rate, and holds it until its response body is
  **closed**, not until the head arrives, since the server is still busy with a body that streams. Every path that
  drops a response closes it, and a response that arrives already read gives its slot back at once, so a slot cannot
  leak.
- **What spends.** Every attempt spends its own turn, retries and hedges included, as do redirect hops and token-mint
  hops. A cache hit spends nothing: the cache sits above the budget.

`budget.effective_rate`, `budget.in_flight` and `budget.pause_remaining` read the budget's state now.

## Per-path lanes

`TransportPolicy(rate_limits=(RateRule(fragment, rate), ...))` gives the requests whose URL path holds `fragment` a
budget of their own: the first matching rule wins, and a matching request spends from the rule's budget **instead
of** the client's. A rule's `rate` is a `Rate` (a budget per client) or a shared `RateBudget`. The fragment is one or
more whole path segments, matched anywhere in the path: `"/exports"` matches `/v2/exports` and
`/v2/exports/orders.csv`, but not `/v2/exports-archive`. The cache's `PathFragmentFilter` and the policy's `Route`
match the same way.

## Server feedback

Every response is read for what it says about the quota, in this order of precedence:

1. `retry-after-ms` or `Retry-After` on a 429 or 503, read exactly as the [retry layer](retries.md#retry-after) reads
   it, so the retry's wait and the budget's pause end together;
2. `RateLimit` with `RateLimit-Policy` (the IETF draft): an item with `r=0` is an exhausted quota, and its `t` is the
   seconds until it returns; a policy whose quota unit is not requests is skipped, and a malformed field is ignored;
3. `X-RateLimit-Remaining` with `X-RateLimit-Reset`, the de facto pair, when `RateLimit` is absent. The reset is a
   number of seconds or an absolute Unix time (in seconds or milliseconds), told apart by size. A server with several
   windows lists a value per window in each header: the pause is the latest reset among the windows whose count is
   zero, paired by position, so `Remaining: 0, 4000` with `Reset: 1, 3600` pauses for 1 s.

A response that names a pause **pauses the whole budget** until it ends, so every concurrent and later call queues
behind it instead of spending a request on a certain refusal. The calls already waiting their turn keep their order:
the first goes when the pause ends and the rest follow at the rate, ahead of any call that arrived during the pause. A
pause lasts at most an hour, long enough for an hourly quota's reset, so one hostile or mistaken header, such as a
`Retry-After` dated in the year 9999, cannot stop every client sharing the budget for good. A call whose deadline
would pass before the pause ends does not wait: it raises `RateLimitError` at once, with `retry_after` set to the time
left in the pause, `retryable=True`, and `status_code` the status of the response that asked for it: a 200 whose
rate-limit headers said the quota was spent gives `RateLimitError(status_code=200)`.

## Adaptive throttling

On by default (`adaptive=None` turns it off), adaptive throttling is AIMD, as in the AWS SDKs' adaptive mode:

- every 429 or 503, and every response whose rate-limit headers say the quota is spent, multiplies the pacing rate by
  `Adaptive.decrease` (0.5), never below `min_rate` (0.1) times the configured rate. A response to a request sent
  before the last decrease does not decrease it again, so one burst of concurrent refusals counts once;
- every other response below 500 restores `increase` (0.1) times the configured rate for each second since the last
  change, up to the configured rate.

## Events

The budget reports every wait it makes a call take as a `RateLimited` event: reason `paced` (its turn at the configured
rate), `throttled` (its turn at a rate adaptive throttling lowered), or `paused` (a server pause). A response that
pauses the budget is reported once more on its own call, with reason `pushback`. See
[observability](observability.md#events).

## Turning it off

A client built inside `headgate.policy.policy_disabled()` paces at an effectively unlimited rate with no concurrency
cap and no throttling, whatever it was given, a shared budget included. See [testing](testing.md#switching-the-policy-off).
