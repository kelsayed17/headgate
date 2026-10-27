---
title: An async API-client base for Python
hide:
  - toc
---

<div class="hg-hero" markdown>

![headgate logo](assets/logo.png)

<div markdown>

# headgate

**An async API-client base for Python, built on [httpx2](https://pypi.org/project/httpx2/).** Subclass
`AsyncBaseClient`, declare what each endpoint answers, and every client you build gets the same production behaviour.
Each endpoint method stays one line.

[Get started](#install){ .md-button .md-button--primary }
[How it compares](comparison.md){ .md-button }
[:fontawesome-brands-github: GitHub](https://github.com/kelsayed17/headgate){ .md-button }

</div>

</div>

<div class="grid cards" markdown>

-   :lucide-rotate-cw:{ .lg .hg-icon } **Retries that are safe**

    ---

    Idempotent methods only, or a POST under an idempotency key. `Retry-After` is waited exactly, and a budget stops
    retries multiplying an outage.

    [:lucide-arrow-right: Retries](retries.md)

-   :lucide-gauge:{ .lg .hg-icon } **A rate budget that listens**

    ---

    One budget shared by every call, paused by `RateLimit` and `X-RateLimit-*` headers and slowed by pushback.

    [:lucide-arrow-right: Rate budgets](rate-budgets.md)

-   :lucide-timer:{ .lg .hg-icon } **Deadlines and a breaker**

    ---

    One deadline bounds every attempt, wait and read. A per-origin circuit breaker stops calls to a host that is down.

    [:lucide-arrow-right: Deadlines](deadlines.md) · [Circuit breaker](breaker.md)

-   :lucide-key-round:{ .lg .hg-icon } **Credentials that stay home**

    ---

    Credentials are pinned to their origin, an https-to-http redirect is refused, and an SSRF guard is one option away.

    [:lucide-arrow-right: Security](security.md)

-   :lucide-database-zap:{ .lg .hg-icon } **A cache that keeps no secrets**

    ---

    hishel's cache, partitioned by credential, never storing one, and storing only what your reader accepted.

    [:lucide-arrow-right: Caching](caching.md)

-   :lucide-activity:{ .lg .hg-icon } **Errors and events that explain**

    ---

    Typed errors that say what happened and whether to retry, typed events, and an OpenTelemetry observer.

    [:lucide-arrow-right: Errors](errors.md) · [Observability](observability.md)

</div>

## One base class instead of a stack

A production API client usually grows a stack: an HTTP library, a retry library, a rate limiter, a cache, a circuit
breaker, a mocking library, and the glue code that makes them agree on deadlines and credentials. headgate is that
whole stack, built to work as one.

<div class="hg-stack">
  <div class="hg-stack-before">
    <span class="hg-stack-label">The usual stack</span>
    <span class="hg-chip">httpx or aiohttp</span>
    <span class="hg-chip">tenacity or stamina</span>
    <span class="hg-chip">aiolimiter</span>
    <span class="hg-chip">hishel or requests-cache</span>
    <span class="hg-chip">pybreaker</span>
    <span class="hg-chip">httpx-sse</span>
    <span class="hg-chip">respx</span>
    <span class="hg-chip hg-chip-glue">your glue code</span>
  </div>
  <div class="hg-stack-arrow" aria-hidden="true">&rarr;</div>
  <div class="hg-stack-after">
    <img src="assets/logo.png" alt="">
    <strong>headgate</strong>
    <span>one dependency, every layer built to work with the others</span>
  </div>
</div>

Each call your client makes passes through every layer, in an order chosen so they cooperate: a cache hit is never
retried, a retry spends its own rate turn, and one deadline bounds the lot.

<ol class="hg-pipeline">
  <li><strong>Deadline</strong><span>one budget of time for the whole call</span></li>
  <li><strong>Circuit breaker</strong><span>fails fast while the host is down</span></li>
  <li><strong>Auth</strong><span>credentials only for your origin</span></li>
  <li><strong>Retries</strong><span>safe re-sends, <code>Retry-After</code> obeyed</span></li>
  <li><strong>Cache</strong><span>a hit stops here</span></li>
  <li><strong>Rate budget</strong><span>paced, and paused when the server asks</span></li>
  <li><strong>Network</strong><span>HTTP/2, size-capped body</span></li>
  <li><strong>Your reader</strong><span>a typed result, or a typed error</span></li>
</ol>

[:lucide-arrow-right: How headgate compares with 20+ libraries](comparison.md){ .md-button }
[:lucide-arrow-right: The standards it follows](standards.md){ .md-button }

## Install

=== "pip"

    ```bash
    pip install 'headgate[msgspec]'
    ```

=== "uv"

    ```bash
    uv add 'headgate[msgspec]'
    ```

The `msgspec` extra brings the codec the examples use; `headgate[pydantic]` brings pydantic's, and any other library
plugs in as a [codec](codecs.md) of its own. headgate needs **Python 3.14** or newer: endpoint paths are [template
strings](https://peps.python.org/pep-0750/), so each value is escaped into its own path segment by default. It runs on
**httpx2**, pydantic's fork of httpx; see [why httpx2](comparison.md#why-httpx2).

## A real client

A client for a few GitHub REST endpoints. The full version, with its tests, is
[`examples/github_client.py`](https://github.com/kelsayed17/headgate/blob/main/examples/github_client.py).

```python
from http import HTTPStatus

import msgspec

from headgate import AsyncBaseClient, BearerAuth, Expect, HttpOptions, LinkHeader, Rate, RateBudget, nothing
from headgate.msgspec import decoder


class Repository(msgspec.Struct, frozen=True):
    full_name: str
    stargazers_count: int


class Issue(msgspec.Struct, frozen=True):
    number: int
    title: str


REPOSITORY_OR_NONE = Expect(decoder(Repository)).on(HTTPStatus.NOT_FOUND, nothing)
ISSUE_PAGES = LinkHeader(items=lambda page: page)  # each page is a JSON list of issues


class GitHub(AsyncBaseClient):
    def __init__(self, token: str) -> None:
        super().__init__(
            base_url="https://api.github.com",
            rate=RateBudget(Rate.per_second(10), max_in_flight=50),
            auth=BearerAuth(token),
            http=HttpOptions(headers={"Accept": "application/vnd.github+json"}),
        )

    async def repository(self, owner: str, name: str) -> Repository | None:
        return await self.get(t"repos/{owner}/{name}", expect=REPOSITORY_OR_NONE)

    def open_issues(self, owner: str, name: str):
        return self.paginate(t"repos/{owner}/{name}/issues", expect=decoder(list[Issue]), pages=ISSUE_PAGES)
```

```python
async with GitHub(token) as github:
    repository = await github.repository("python", "cpython")
    async for issue in github.open_issues("python", "cpython"):
        print(issue.number, issue.title)
```

Retries, pacing under GitHub's limits, pausing when its hourly limit is spent, following `Link` headers, and keeping
the token on `api.github.com` all come from the base class.

## Where to next

- [Compared with others](comparison.md): what headgate does that tenacity, stamina or a hand-rolled httpx client
  does not, and when to pick those instead.
- [Configuration](configuration.md): every option, and where it lives.
- [Declared responses](responses.md) and [errors](errors.md): what an endpoint answers, and what it raises.
- [Testing a client](testing.md): fake networks, instant retries and failure schedules, with nothing private patched.
