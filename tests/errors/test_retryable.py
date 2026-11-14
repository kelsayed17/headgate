# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``retryable`` has one meaning: sending the call again later could succeed, and is safe as the call stands.

The retry policy's verdict decides it for a status or a network failure: the outcome is one the policy re-sends after,
and the method is idempotent, the call is keyed, or nothing of it was sent. Each refusal headgate makes itself before
the request leaves says ``True`` with its ``retry_after``; an error sending again cannot cure says ``False``. The two
rate-budget refusals are pinned beside the budget's own tests (``tests/ratelimit/test_client_feedback.py`` and
``tests/integration/test_deadline.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import (
    BreakerPolicy,
    Client,
    ClientError,
    HttpOptions,
    Json,
    SecurityOptions,
    TransportPolicy,
    UpstreamStatusError,
    as_bytes,
    body_reader,
    nothing,
)
from headgate.breaker import BreakerRegistry
from headgate.errors import (
    AuthError,
    CircuitOpenError,
    ForbiddenDestinationError,
    PageLimitError,
    RateLimitError,
    ResponseParseError,
    ResponseTooLargeError,
)
from headgate.pagination import Offset
from headgate.retry import Backoff, RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_BASE = "https://api.example.test"
#: Retries that never wait, so a surviving status is reached at once.
_ONCE_MORE = RetryPolicy(attempts=2, backoff=Backoff(factor=0.0))
#: An upload's body.
_ORDER = Json({"qty": 1})


def _answering(status: int) -> httpx2.MockTransport:
    """Return a transport answering every request *status*, with a small body."""
    return httpx2.MockTransport(lambda _request: httpx2.Response(status, content=b"[1]"))


def _client(status: int, *, http: HttpOptions | None = None, breaker: BreakerPolicy | None = None) -> Client:
    """Return a client whose every request is answered *status*, retried once without waiting."""
    return Client(
        base_url=_BASE,
        policy=TransportPolicy(retry=_ONCE_MORE, breaker=breaker),
        http=http or HttpOptions(transport=_answering(status)),
    )


async def _raised(call: Awaitable[object]) -> ClientError:
    """Return the ``ClientError`` *call* raises."""
    with pytest.raises(ClientError) as raised:
        await call
    return raised.value


def _rejected(_body: bytes) -> object:
    """Fail as a reader whose body is not what it reads."""
    msg = "not an order"
    raise ValueError(msg)


_CASES: list[tuple[str, Callable[[], Awaitable[object]], type[ClientError], bool, bool | None]] = [
    ("GET 503", lambda: _client(503).get("orders", expect=as_bytes), UpstreamStatusError, True, True),
    ("GET 429", lambda: _client(429).get("orders", expect=as_bytes), RateLimitError, True, True),
    ("GET 401", lambda: _client(401).get("orders", expect=as_bytes), AuthError, False, True),
    (
        "unkeyed POST 503",
        lambda: _client(503).post("orders", expect=nothing, body=_ORDER),
        UpstreamStatusError,
        False,
        True,
    ),
    (
        "keyed POST 503",
        lambda: _client(503).post("orders", expect=nothing, body=_ORDER, idempotency_key="k"),
        UpstreamStatusError,
        True,
        True,
    ),
    (
        "unreadable body",
        lambda: _client(200).get("orders", expect=body_reader(_rejected)),
        ResponseParseError,
        False,
        True,
    ),
    (
        "body too large",
        lambda: _client(200, http=HttpOptions(transport=_answering(200), max_response_bytes=1)).get(
            "orders", expect=as_bytes
        ),
        ResponseTooLargeError,
        False,
        True,
    ),
    (
        "private destination",
        lambda: Client(
            base_url="http://127.0.0.1",
            http=HttpOptions(transport=_answering(200), security=SecurityOptions(block_private_hosts=True)),
        ).get("orders", expect=as_bytes),
        ForbiddenDestinationError,
        False,
        False,
    ),
]


@pytest.mark.parametrize(("_name", "call", "kind", "retryable", "sent"), _CASES, ids=[case[0] for case in _CASES])
async def test_retryable_and_sent_for_each_kind_of_failure(
    _name: str, call: Callable[[], Awaitable[object]], kind: type[ClientError], *, retryable: bool, sent: bool | None
) -> None:
    """Say ``retryable`` exactly when sending again could succeed and is safe, and ``sent`` as recorded."""
    error = await _raised(call())

    assert type(error) is kind
    assert (error.retryable, error.sent) == (retryable, sent)


#: How long an open breaker here stays open.
_COOLDOWN = 3600.0


def _broken(status: int = 503) -> Client:
    """Return a client whose breaker opens on its first failure and stays open for :data:`_COOLDOWN`."""
    breaker = BreakerPolicy(failure_threshold=1, recovery_timeout=_COOLDOWN, registry=BreakerRegistry())
    return _client(status, breaker=breaker)


async def test_an_open_breaker_is_retryable_after_its_cooldown() -> None:
    """Say ``retryable`` for a call the breaker refused unsent, with the cooldown left as ``retry_after``."""
    client = _broken()
    await _raised(client.get("orders", expect=as_bytes))

    error = await _raised(client.post("orders", expect=nothing, body=_ORDER))

    assert type(error) is CircuitOpenError
    assert (error.retryable, error.sent) == (True, False)
    assert error.retry_after is not None
    assert 0 < error.retry_after <= _COOLDOWN


async def test_a_walk_past_its_page_limit_is_not_retryable_and_sent_nothing_of_its_own() -> None:
    """Say a page budget's refusal is not retryable, and leave ``sent`` unknown: no send of it failed."""
    transport = httpx2.MockTransport(lambda _request: httpx2.Response(200, json=[1]))
    client = Client(base_url=_BASE, http=HttpOptions(transport=transport))
    pages = Offset(items=lambda page: page, page_size=1)

    with pytest.raises(PageLimitError) as raised:
        await client.paginate("orders", expect=body_reader(lambda body: [body]), pages=pages, max_pages=1).collect()

    assert (raised.value.retryable, raised.value.sent) == (False, None)


async def _resilient(
    call: Callable[[], Awaitable[object]], rescheduled: list[float | None], reported: list[str | None]
) -> None:
    """Make *call* as ``docs/errors.md``'s "Reading a failure" does, recording each reschedule and report."""
    try:
        await call()
    except ClientError as exc:
        if not exc.retryable:
            if isinstance(exc, UpstreamStatusError) and exc.problem is not None:
                reported.append(exc.problem.title)
            raise
        rescheduled.append(exc.retry_after)


async def test_the_docs_recipe_reschedules_what_can_be_sent_again_and_raises_the_rest() -> None:
    """Reschedule an open breaker's refusal and a surviving GET 503, and re-raise a surviving unkeyed POST 503."""
    rescheduled: list[float | None] = []
    reported: list[str | None] = []
    breaker = _broken()
    await _raised(breaker.get("orders", expect=as_bytes))

    await _resilient(lambda: breaker.get("orders", expect=as_bytes), rescheduled, reported)
    await _resilient(lambda: _client(503).get("orders", expect=as_bytes), rescheduled, reported)
    with pytest.raises(UpstreamStatusError) as raised:
        await _resilient(lambda: _client(503).post("orders", expect=nothing, body=_ORDER), rescheduled, reported)

    assert len(rescheduled) == len(("breaker", "GET"))
    assert rescheduled[0] is not None, "the breaker says when to come back"
    assert raised.value.sent is True, "the POST may have been processed"
    assert reported == [], "no problem detail was sent"


async def test_the_docs_recipe_reports_a_problem_detail_and_raises_it() -> None:
    """Report a refusal's RFC 9457 title, and re-raise it: a 400 is not sent again."""
    problem = httpx2.Response(
        400, json={"title": "out of credit"}, headers={"Content-Type": "application/problem+json"}
    )
    client = Client(base_url=_BASE, http=HttpOptions(transport=httpx2.MockTransport(lambda _request: problem)))
    rescheduled: list[float | None] = []
    reported: list[str | None] = []

    with pytest.raises(UpstreamStatusError):
        await _resilient(lambda: client.get("orders", expect=as_bytes), rescheduled, reported)

    assert (rescheduled, reported) == ([], ["out of credit"])
