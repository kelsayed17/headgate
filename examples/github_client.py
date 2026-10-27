# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A client for a few GitHub REST endpoints: the API everyone knows, written as a headgate client.

GitHub needs a bearer token, two fixed headers, pagination by ``Link`` header, a 404 for a repository that does not
exist (or that the token cannot see), and pacing under its limits. headgate covers each with a declaration, so every
endpoint method stays one line:

* the rate budget reads GitHub's ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` headers by default, and pauses every
  call until the reset when the hourly limit is spent, instead of spending requests on certain refusals;
* ``max_in_flight`` keeps under GitHub's limit on concurrent requests;
* the bearer token never leaves ``api.github.com``, even when a ``Link`` header points elsewhere.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import msgspec

from headgate import AsyncBaseClient, BearerAuth, Expect, HttpOptions, LinkHeader, Rate, RateBudget, nothing
from headgate.msgspec import decoder

if TYPE_CHECKING:
    import httpx2

    from headgate import Paginated

API = "https://api.github.com"


class Repository(msgspec.Struct, frozen=True):
    """The fields of a repository this client reads; GitHub sends many more, which are ignored."""

    full_name: str
    description: str | None
    stargazers_count: int
    default_branch: str


class Issue(msgspec.Struct, frozen=True):
    """An issue. GitHub lists pull requests as issues too; ``pull_request`` is set on those."""

    number: int
    title: str
    pull_request: dict[str, object] | None = None


def _issues(page: list[Issue]) -> list[Issue]:
    """Return the issues on a page, which is a JSON list."""
    return page


# Readers and pagers are values: build them once, at import, where a bad declaration fails.
REPOSITORY = decoder(Repository)
REPOSITORY_OR_NONE = Expect(REPOSITORY).on(HTTPStatus.NOT_FOUND, nothing)  # a missing or hidden repository
ISSUES = decoder(list[Issue])
ISSUE_PAGES = LinkHeader(items=_issues)


class GitHub(AsyncBaseClient):
    """Read repositories and their open issues from the GitHub REST API."""

    def __init__(self, token: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        """Authenticate with *token*; *transport* replaces the network, as a test does."""
        super().__init__(
            base_url=API,
            # Well under the 5,000 requests an hour a token gets, and never more than 50 at once.
            rate=RateBudget(Rate.per_second(10), max_in_flight=50),
            auth=BearerAuth(token),
            http=HttpOptions(
                transport=transport,
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            ),
        )

    async def repository(self, owner: str, name: str) -> Repository | None:
        """Return the repository *owner*/*name*, or ``None`` when it does not exist or the token cannot see it."""
        return await self.get(t"repos/{owner}/{name}", expect=REPOSITORY_OR_NONE)

    def open_issues(self, owner: str, name: str) -> Paginated[list[Issue], Issue]:
        """Return the open issues (and pull requests) of *owner*/*name*, 100 a page, fetched as they are iterated."""
        params = {"state": "open", "per_page": 100}
        return self.paginate(t"repos/{owner}/{name}/issues", expect=ISSUES, pages=ISSUE_PAGES, params=params)
