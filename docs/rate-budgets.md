# Rate budgets

Every request a client sends to the network spends from a **rate budget**: it waits its turn at the configured rate,
optionally for a free concurrency slot, and for any pause the server asked for. Waiting on the client side is cheaper
than collecting 429s, and a budget that listens to the server stops a fleet of callers from hammering an API that has
said "slow down".

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

`Rate(requests, period)` is a value: `Rate.per_second(5)` and `Rate.per_minute(300)` are the same rate. A client given
a `Rate` builds a budget of its own. A `RateBudget` is the budget as an object: pass one instance to several clients
(or rules) and they spend from it together, across tasks, event loops and threads, because its state is plain numbers
under a `threading.Lock` and nothing in it is bound to a loop.

`RateBudget(rate, *, max_in_flight=None, adaptive=Adaptive(), timing=..., wall_clock=time.time)`:

- **Pacing** is GCRA, the virtual-scheduling form of a leaky bucket. Up to `rate.requests` requests (at least one) go
  out back to back; after that they are `period / requests` seconds apart. Waiters are served in arrival order, and a
  waiter cancelled in the queue (its deadline passed, say) gives its turn back when nobody reserved after it.
- **`max_in_flight`** caps how many requests are open at once. A request holds its slot from the send until its
  response body is **closed**, not until the head arrives, since the server is still busy with a body that streams.
  Every path that drops a response closes it, so a slot cannot leak.
- **What spends.** Every attempt spends its own turn, retries and hedges included, as do redirect hops and token-mint
  hops. A cache hit spends nothing: the cache sits above the budget.

`budget.effective_rate`, `budget.in_flight` and `budget.pause_remaining()` read the budget's state now.

## Per-path lanes

`TransportPolicy(rate_limits=(RateRule(fragment, rate), ...))` gives the requests whose URL path contains `fragment`
a budget of their own: the first matching rule wins, and a matching request spends from the rule's budget **instead
of** the client's. A rule's `rate` is a `Rate` (a budget per client) or a shared `RateBudget`. The fragment is a plain
substring of the path, like the cache's `PathFragmentFilter` and the policy's `Route`.

## Server feedback

Every response is read for what it says about the quota, in this order of precedence:

1. `retry-after-ms` or `Retry-After` on a 429 or 503, read exactly as the [retry layer](retries.md#retry-after) reads
   it, so the retry's wait and the budget's pause end together;
2. `RateLimit` with `RateLimit-Policy` (the IETF draft): an item with `r=0` is an exhausted quota, and its `t` is the
   seconds until it returns; a policy whose quota unit is not requests is skipped, and a malformed field is ignored;
3. `X-RateLimit-Remaining` with `X-RateLimit-Reset`, the de facto pair, when `RateLimit` is absent. The reset is a
   number of seconds or an absolute Unix time (in seconds or milliseconds), told apart by size.

A response that names a pause **pauses the whole budget** until it ends, so every concurrent and later call queues
behind it instead of spending a request on a certain refusal. A call whose deadline would pass before the pause ends
does not wait: it raises `RateLimitError` at once, with `retry_after` set to the time left in the pause and
`status_code` the status of the response that asked for it.

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
