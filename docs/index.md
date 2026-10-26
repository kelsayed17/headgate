# headgate

**An async API-client base for Python, built on [httpx2](https://pypi.org/project/httpx2/).** Subclass
`AsyncBaseClient`, declare what each endpoint answers, and every client you build gets the same production behaviour:

- **retries that re-send only when it is safe**: idempotent methods, or a POST under an idempotency key, with
  `Retry-After` honoured and a budget across calls;
- **a shared rate budget that listens to the server**: `RateLimit` and `X-RateLimit-*` headers pause it, and pushback
  slows it down;
- **a per-origin circuit breaker** and **whole-call deadlines** that bound every wait and retry;
- **credentials that never leave your origin**, and an opt-in **cache that never stores them**;
- **typed events and OpenTelemetry**, and **errors that say exactly what happened**.

Each endpoint method stays one line.

## Install

```bash
pip install headgate   # or: uv add headgate
```

headgate needs **Python 3.14** or newer: endpoint paths are [template strings](https://peps.python.org/pep-0750/), so
each value is escaped into its own path segment by default. It runs on **httpx2**, pydantic's fork of httpx; see
[why httpx2](comparison.md#why-httpx2).

## A real client

A client for a few GitHub REST endpoints. The full version, with its tests, is
[`examples/github_client.py`](https://github.com/kelsayed17/headgate/blob/main/examples/github_client.py).

```python
from http import HTTPStatus

import msgspec

from headgate import AsyncBaseClient, Expect, HttpOptions, LinkHeader, Rate, RateBudget, StaticHeaderAuth, nothing
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
            auth=StaticHeaderAuth({"Authorization": f"Bearer {token}"}),
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
