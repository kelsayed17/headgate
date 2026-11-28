# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every value a client is configured with refuses a value of the wrong kind where it is built, naming the field.

A ``bool`` is an ``int`` to ``isinstance`` and to a type checker, so ``Timeouts(read=True)`` and ``Rate(True, 1)``
type-check; a ``str`` is what an untyped config file hands over. Each is a ``TypeError`` naming the field, what it
takes and what it got, rather than a comparison error from inside the constructor or a value silently accepted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

import pytest

from headgate import (
    BreakerPolicy,
    Client,
    Expect,
    HttpCache,
    HttpOptions,
    Rate,
    RateBudget,
    RetryPolicy,
    Timeouts,
    as_json,
    nothing,
)
from headgate.breaker import BreakerRegistry
from headgate.msgspec import reader
from headgate.pagination import BodyCursor, BodyOffset, Cursor, LinkHeader, NextUrl, Offset, PageNumber
from headgate.ratelimit import Adaptive
from headgate.retry import Backoff, Hedge, RetryShare
from headgate.testing import DropMidBody, FakeTransport, Respond, Slow, TestClock

if TYPE_CHECKING:
    from collections.abc import Callable

_PAGE: Final = reader(list[int])

#: ``True``, typed as anything, for a field that takes no ``bool``.
_TRUE: Final[Any] = True

#: A status a polling API answers while a job runs.
_ACCEPTED: Final = 202


def _items(page: list[int]) -> list[int]:
    return page


#: Each numeric field, the name its refusal gives it, a builder of a value given it, and whether it takes an int only.
_FIELDS: Final[list[tuple[str, Callable[[Any], object], bool]]] = [
    ("connect", lambda v: Timeouts(connect=v), False),
    ("read", lambda v: Timeouts(read=v), False),
    ("write", lambda v: Timeouts(write=v), False),
    ("pool", lambda v: Timeouts(pool=v), False),
    ("requests", lambda v: Rate(v, 1.0), False),
    ("period", lambda v: Rate(1.0, v), False),
    ("max_in_flight", lambda v: RateBudget(Rate.per_second(1), max_in_flight=v), True),
    ("attempts", lambda v: RetryPolicy(attempts=v), True),
    ("max_retry_after", lambda v: RetryPolicy(max_retry_after=v), False),
    ("min_attempt_time", lambda v: RetryPolicy(min_attempt_time=v), False),
    ("factor", lambda v: Backoff(factor=v), False),
    ("max_wait", lambda v: Backoff(max_wait=v), False),
    ("after", lambda v: Hedge(after=v), False),
    ("max_extra", lambda v: Hedge(max_extra=v), True),
    ("ratio", lambda v: RetryShare(ratio=v), False),
    ("min_per_second", lambda v: RetryShare(min_per_second=v), False),
    ("window", lambda v: RetryShare(window=v), False),
    ("decrease", lambda v: Adaptive(decrease=v), False),
    ("increase", lambda v: Adaptive(increase=v), False),
    ("min_rate", lambda v: Adaptive(min_rate=v), False),
    ("failure_threshold", lambda v: BreakerPolicy(failure_threshold=v), True),
    ("recovery_timeout", lambda v: BreakerPolicy(recovery_timeout=v), False),
    ("max_hosts", lambda v: BreakerRegistry(max_hosts=v), True),
    ("ttl", lambda v: HttpCache(storage=cast("Any", object()), ttl=v), False),
    ("page_size", lambda v: Offset(_PAGE, items=_items, page_size=v), True),
    ("start", lambda v: Offset(_PAGE, items=_items, page_size=10, start=v), True),
    ("start", lambda v: PageNumber(_PAGE, items=_items, more=bool, start=v), True),
    ("page_size", lambda v: BodyOffset(_PAGE, items=_items, page_size=v, offset=len, put=lambda b, _: b), True),
    ("after_bytes", DropMidBody, True),
    ("status_code", Respond, True),
    ("retry_after", lambda v: Respond(503, retry_after=v), False),
    ("seconds", Slow, False),
    ("start", TestClock, False),
    ("epoch", lambda v: TestClock(epoch=v), False),
]

_CASES: Final = [
    (name, build, value) for name, build, whole in _FIELDS for value in (True, "5", *((2.5,) if whole else ()))
]


@pytest.mark.parametrize(
    ("name", "build", "value"),
    _CASES,
    ids=[f"{index // 3}-{name}-{type(value).__name__}" for index, (name, _, value) in enumerate(_CASES)],
)
def test_a_number_is_never_a_bool_or_text(name: str, build: Callable[[Any], object], value: object) -> None:
    """Refuse ``True``, ``"5"`` and, where a whole number goes, ``2.5``, naming the field and what it got."""
    with pytest.raises(TypeError, match=rf"^{name}= takes .*\(got {type(value).__name__}\)$"):
        build(value)


def test_a_rate_budget_takes_a_rate() -> None:
    """Refuse a bare number where a ``Rate`` goes, naming what to write."""
    with pytest.raises(TypeError, match=r"^rate= takes a Rate, such as Rate.per_second\(10\) \(got int\)$"):
        RateBudget(cast("Any", 5))


def test_a_rate_budget_takes_an_adaptive_or_none() -> None:
    """Refuse an ``adaptive=`` that is neither an ``Adaptive`` nor ``None``."""
    with pytest.raises(TypeError, match=r"^adaptive= takes an Adaptive, or None"):
        RateBudget(Rate.per_second(1), adaptive=_TRUE)


@pytest.mark.parametrize(
    ("statuses", "error", "message"),
    [
        (frozenset({"500"}), TypeError, r"\(got str '500' among them\)"),
        (frozenset({True}), TypeError, r"\(got bool True among them\)"),
        (frozenset({999}), ValueError, r"100 to 599 \(got 999\)"),
    ],
    ids=["text", "bool", "range"],
)
def test_retry_statuses_are_http_statuses(statuses: frozenset[int], error: type[Exception], message: str) -> None:
    """Refuse a status no response equals, which would never match, or one outside 100-599."""
    with pytest.raises(error, match=rf"^statuses= .*{message}"):
        RetryPolicy(statuses=statuses)


def test_a_retry_policy_may_retry_a_202() -> None:
    """Accept a 2xx, which a polling API answers while a job runs."""
    assert _ACCEPTED in RetryPolicy(statuses=frozenset({_ACCEPTED})).statuses


def test_cache_statuses_are_ints() -> None:
    """Refuse a numeric string among the cache's statuses, before its 2xx rule compares it."""
    with pytest.raises(TypeError, match=r"^statuses= .*\(got str '200' among them\)"):
        HttpCache(storage=cast("Any", object()), statuses=cast("Any", ("200",)))


@pytest.mark.parametrize("status", ["404", True], ids=["text", "bool"])
def test_an_answer_is_declared_on_an_int(status: object) -> None:
    """Refuse a status given as text or as a ``bool``, naming ``status=``."""
    with pytest.raises(TypeError, match=rf"^status= takes an HTTP status, an int .*\(got {type(status).__name__}\)$"):
        Expect(as_json).on(cast("Any", status), nothing)


class _Misspelled:
    """An observer whose method is not named ``notify``."""

    def on_event(self, event: object) -> None:
        """Never called."""
        del event


@pytest.mark.parametrize("observer", [print, _Misspelled()], ids=["function", "misspelled"])
def test_an_observer_has_a_notify_method(observer: object) -> None:
    """Refuse an observer with no callable ``notify``, whose every event would fail into ``observer_failures()``."""
    with pytest.raises(TypeError, match=r"^observers= takes Observers, each with a notify\(event\) method"):
        Client(base_url="https://api.example.test/", observers=cast("Any", [observer]))


_PAGERS: Final[list[tuple[str, Callable[[], object], str]]] = [
    ("cursor-read", lambda: Cursor(cast("Any", 5), items=_items, next=str, param="c"), "read"),
    ("cursor-items", lambda: Cursor(_PAGE, items=cast("Any", 5), next=str, param="c"), "items"),
    ("cursor-next", lambda: Cursor(_PAGE, items=_items, next=cast("Any", "c"), param="c"), "next"),
    ("offset-read", lambda: Offset(cast("Any", None), items=_items, page_size=10), "read"),
    ("offset-total", lambda: Offset(_PAGE, items=_items, page_size=10, total=cast("Any", 5)), "total"),
    ("number-more", lambda: PageNumber(_PAGE, items=_items, more=_TRUE), "more"),
    ("link-read", lambda: LinkHeader(cast("Any", list), items=_items), "read"),
    ("link-items", lambda: LinkHeader(_PAGE, items=cast("Any", [])), "items"),
    ("next-read", lambda: NextUrl(cast("Any", "page"), items=_items, next=str), "read"),
    ("body-cursor-put", lambda: BodyCursor(_PAGE, items=_items, next=str, put=cast("Any", 1)), "put"),
    (
        "body-offset-offset",
        lambda: BodyOffset(_PAGE, items=_items, page_size=5, offset=cast("Any", 0), put=max),
        "offset",
    ),
    ("cursor-write", lambda: Cursor(_PAGE, items=_items, next=str, param="c", write=cast("Any", "json")), "write"),
]


@pytest.mark.parametrize(("build", "field"), [(b, f) for _, b, f in _PAGERS], ids=[i for i, _, _ in _PAGERS])
def test_a_pager_checks_its_reader_and_hooks_when_built(build: Callable[[], object], field: str) -> None:
    """Refuse a page reader that is no ``Reader`` or ``Expect``, and a hook that cannot be called, where it is built."""
    with pytest.raises(TypeError, match=rf"^{field}= takes "):
        build()


async def test_paginate_refuses_what_is_no_pager() -> None:
    """Refuse a ``pager=`` without the pager's methods, naming the first it lacks, before reading its ``read``."""
    client = Client(base_url="https://api.example.test/", http=HttpOptions(transport=FakeTransport()))

    class _HalfPager:
        read = _PAGE

    for pager, missing in ((None, "first"), (_HalfPager(), "first")):
        with pytest.raises(
            TypeError, match=rf"^pager= takes a Pager, such as Cursor\(reader\(Page\), .*has no {missing}"
        ):
            client.paginate("x", pager=cast("Any", pager))
