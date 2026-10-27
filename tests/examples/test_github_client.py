# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``examples/github_client.py``: GitHub's headers, a 404 answer, ``Link`` pagination and its rate-limit headers."""

from __future__ import annotations

import time

import github_client as example
import httpx2
import pytest

from headgate import RateLimitError
from headgate.testing import FakeTransport

_REPO = {"full_name": "octo/hello", "description": None, "stargazers_count": 42, "default_branch": "main", "id": 1}
_PAGE_2 = f"{example.API}/repositories/1/issues?state=open&per_page=100&page=2"
#: Seconds until GitHub's hourly limit resets, in the spent-limit test; the call's 60 s deadline cannot wait that long.
_RESET_IN = 3600


async def test_a_repository_is_read_with_githubs_headers() -> None:
    """Read a repository, sending the token and GitHub's media type and API version."""
    fake = FakeTransport().reply("GET", "/repos/octo/hello", httpx2.Response(200, json=_REPO))
    async with example.GitHub("t0ken", transport=fake) as github:
        repository = await github.repository("octo", "hello")

    assert repository == example.Repository("octo/hello", None, 42, "main")
    (sent,) = fake.calls
    assert sent.headers["authorization"] == "Bearer t0ken"
    assert sent.headers["accept"] == "application/vnd.github+json"
    assert sent.headers["x-github-api-version"] == "2022-11-28"


async def test_a_missing_repository_is_none() -> None:
    """Answer ``None`` for the 404 GitHub sends for a repository that does not exist or that the token cannot see."""
    fake = FakeTransport().reply("GET", "/repos/octo/gone", httpx2.Response(404, json={"message": "Not Found"}))
    async with example.GitHub("t0ken", transport=fake) as github:
        assert await github.repository("octo", "gone") is None


async def test_open_issues_follow_the_link_header() -> None:
    """Follow ``rel="next"`` from page to page, asking for open issues 100 at a time."""
    first = httpx2.Response(
        200,
        json=[{"number": 2, "title": "Crash"}, {"number": 1, "title": "Docs", "pull_request": {"url": "u"}}],
        headers={"Link": f'<{_PAGE_2}>; rel="next", <{_PAGE_2}>; rel="last"'},
    )
    last = httpx2.Response(200, json=[{"number": 0, "title": "First"}])
    fake = FakeTransport().reply("GET", "/repos/octo/hello/issues", first).reply("GET", "/repositories/1/issues", last)
    async with example.GitHub("t0ken", transport=fake) as github:
        issues = [issue async for issue in github.open_issues("octo", "hello")]

    assert [issue.number for issue in issues] == [2, 1, 0]
    assert [issue.pull_request is not None for issue in issues] == [False, True, False]
    first_sent, second_sent = fake.calls
    assert first_sent.url.params["state"] == "open"
    assert first_sent.url.params["per_page"] == "100"
    assert second_sent.url.params["page"] == "2"
    assert second_sent.headers["authorization"] == "Bearer t0ken", "the next page is on GitHub, so it is authenticated"


@pytest.mark.transport_policy
async def test_a_spent_hourly_limit_pauses_every_call_until_the_reset() -> None:
    """Stop sending once GitHub reports the hourly limit spent: a call that cannot wait for the reset fails at once."""
    spent = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + _RESET_IN)}
    fake = FakeTransport().reply("GET", "/repos/octo/hello", httpx2.Response(200, json=_REPO, headers=spent))
    async with example.GitHub("t0ken", transport=fake) as github:
        await github.repository("octo", "hello")
        with pytest.raises(RateLimitError) as raised:
            await github.repository("octo", "hello")

    assert len(fake.calls) == 1, "the second call was never sent"
    assert raised.value.retry_after is not None
    assert raised.value.retry_after > _RESET_IN - 60, "the pause runs until the reset"
