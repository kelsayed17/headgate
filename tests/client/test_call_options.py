# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every verb, ``stream`` and ``paginate`` thread a call's own ``retry=`` and ``follow_redirects=`` to its send.

An option left out inherits, ``None`` is off, and a keyword or value the options do not take is refused before anything
is sent.

A scripted network answers a retryable 503 then a 200, or a redirect, so each option's effect shows in what was sent
and what the call returned or raised. A POST or PATCH carries an idempotency key, so the policy may re-send it.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Final, cast

import httpx2
import pytest

from headgate import HttpOptions, Rate, RetryPolicy, TransportPolicy, UpstreamStatusError, nothing
from headgate.pagination import NextUrl
from tests.support.clients import StubClient
from tests.support.wire import Script

if TYPE_CHECKING:
    from headgate import Method

_BASE: Final = "https://api.example.test"
_PATH: Final = "orders/1"
_KEY: Final = "key-1"
_VERBS: Final[tuple[Method, ...]] = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
#: A 503 the default policy re-sends, then the answer.
_FLAKY: Final = (503, 200)


def _no_items(_page: None) -> tuple[()]:
    """Return no items: the pages here carry none."""
    return ()


def _no_next(_page: None) -> None:
    """Name no next page."""


_ONE_PAGE: Final = NextUrl[None, object](items=_no_items, next=_no_next)


def _client(script: Script) -> StubClient:
    """Return a client over *script* under the default retry policy and no breaker."""
    return StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        policy=TransportPolicy(breaker=None),
        http=HttpOptions(transport=script.transport()),
    )


def _moved() -> Script:
    """Return a network that redirects every request."""
    return Script(httpx2.Response(302, headers={"Location": f"{_BASE}/elsewhere"}))


async def _by_default(client: StubClient, method: Method) -> None:
    """Send *method* to :data:`_PATH` with the call's defaults."""
    match method:
        case "GET":
            await client.get(_PATH, expect=nothing)
        case "POST":
            await client.post(_PATH, expect=nothing, idempotency_key=_KEY)
        case "PUT":
            await client.put(_PATH, expect=nothing)
        case "PATCH":
            await client.patch(_PATH, expect=nothing, idempotency_key=_KEY)
        case "DELETE":
            await client.delete(_PATH, expect=nothing)
        case _:
            await client.head(_PATH, expect=nothing)


async def _without_retry(client: StubClient, method: Method) -> None:
    """Send *method* to :data:`_PATH` with ``retry=None``."""
    match method:
        case "GET":
            await client.get(_PATH, expect=nothing, retry=None)
        case "POST":
            await client.post(_PATH, expect=nothing, idempotency_key=_KEY, retry=None)
        case "PUT":
            await client.put(_PATH, expect=nothing, retry=None)
        case "PATCH":
            await client.patch(_PATH, expect=nothing, idempotency_key=_KEY, retry=None)
        case "DELETE":
            await client.delete(_PATH, expect=nothing, retry=None)
        case _:
            await client.head(_PATH, expect=nothing, retry=None)


async def _not_following(client: StubClient, method: Method) -> None:
    """Send *method* to :data:`_PATH` with ``follow_redirects=False``."""
    match method:
        case "GET":
            await client.get(_PATH, expect=nothing, follow_redirects=False)
        case "POST":
            await client.post(_PATH, expect=nothing, follow_redirects=False)
        case "PUT":
            await client.put(_PATH, expect=nothing, follow_redirects=False)
        case "PATCH":
            await client.patch(_PATH, expect=nothing, follow_redirects=False)
        case "DELETE":
            await client.delete(_PATH, expect=nothing, follow_redirects=False)
        case _:
            await client.head(_PATH, expect=nothing, follow_redirects=False)


@pytest.mark.parametrize("method", _VERBS)
async def test_every_verb_retries_by_default(method: Method) -> None:
    """Re-send a 503 under the client's policy when the call says nothing about retries."""
    script = Script(*_FLAKY)

    await _by_default(_client(script), method)

    assert [request.method for request in script.sent] == [method, method]


@pytest.mark.parametrize("method", _VERBS)
async def test_every_verb_sends_once_with_retry_none(method: Method) -> None:
    """Send once with ``retry=None``, raising the 503 with the call's own method."""
    script = Script(*_FLAKY)

    with pytest.raises(UpstreamStatusError) as raised:
        await _without_retry(_client(script), method)

    assert len(script.sent) == 1
    assert (raised.value.method, raised.value.status_code) == (method, 503)


@pytest.mark.parametrize("method", _VERBS)
async def test_every_verb_raises_a_redirect_it_does_not_follow(method: Method) -> None:
    """Raise the 302 itself with ``follow_redirects=False``, sending nothing to where it points."""
    script = _moved()

    with pytest.raises(UpstreamStatusError) as raised:
        await _not_following(_client(script), method)

    assert len(script.sent) == 1
    assert raised.value.status_code == httpx2.codes.FOUND


async def test_a_stream_sends_once_with_retry_none() -> None:
    """Open a stream with ``retry=None`` with one send, raising the 503."""
    script = Script(*_FLAKY)

    with pytest.raises(UpstreamStatusError):
        async with _client(script).stream("events", retry=None):
            pass

    assert len(script.sent) == 1


async def test_a_streams_last_event_id_joins_the_calls_own_headers() -> None:
    """Send ``Last-Event-ID`` beside the headers the call passed, not instead of them."""
    script = Script()

    async with _client(script).stream("events", headers={"X-Trace": "t-1"}, last_event_id="41"):
        pass

    sent = script.sent[0].headers
    assert (sent["x-trace"], sent["last-event-id"]) == ("t-1", "41")


async def test_paginate_refuses_a_request_body_with_its_reason() -> None:
    """Refuse a body option no pager can rewrite on ``paginate``, naming the ones a walk takes."""
    message = r"^paginate\(\) got an unexpected keyword argument 'data': a walk of POSTs sends body= or json=$"
    form: dict[str, Any] = {"data": {"a": 1}}
    with pytest.raises(TypeError, match=message):
        _client(Script()).paginate("orders", expect=nothing, pages=_ONE_PAGE, **form)


async def test_paginate_sends_each_page_once_with_retry_none() -> None:
    """Fetch a page with one send under ``retry=None``, raising the 503."""
    script = Script(*_FLAKY)
    pages = _client(script).paginate("orders", expect=nothing, pages=_ONE_PAGE, retry=None)

    with pytest.raises(UpstreamStatusError):
        await anext(aiter(pages.pages()))

    assert len(script.sent) == 1


async def test_retry_none_sends_once_and_absence_inherits() -> None:
    """Send once for a call's ``retry=None`` on a retrying view, and take the view's policy when ``retry`` is left out.

    Before ``retry`` was an option, ``retry=None`` meant "no override", so the first call here was retried.
    """
    script = Script(503, 503, 503, 200)
    view = _client(script).with_options(retry=RetryPolicy(attempts=3))

    with pytest.raises(UpstreamStatusError):
        await view.get(_PATH, expect=nothing, retry=None)
    assert len(script.sent) == 1

    await view.get(_PATH, expect=nothing)
    assert len(script.sent) == len(("the first call's 503", "503", "503", "200"))


async def test_a_view_follows_redirects_as_it_says() -> None:
    """Raise a redirect as its 3xx on a view saying ``follow_redirects=False``, and follow it when the call says so."""
    moved = httpx2.Response(302, headers={"Location": f"{_BASE}/elsewhere"})
    script = Script(moved, moved, 200)
    view = _client(script).with_options(follow_redirects=False)

    with pytest.raises(UpstreamStatusError) as raised:
        await view.get(_PATH, expect=nothing)
    assert raised.value.status_code == httpx2.codes.FOUND

    await view.get(_PATH, expect=nothing, follow_redirects=True)
    assert [request.url.path for request in script.sent] == ["/orders/1", "/orders/1", "/elsewhere"]


def _accept_all(_response: httpx2.Response) -> None:
    """Refuse nothing: a response check that only has to exist."""


def test_a_view_cannot_follow_redirects_under_a_response_check() -> None:
    """Refuse a following view on a client with a ``response_check``, as every call of it would be refused."""
    client = StubClient(
        base_url=_BASE,
        rate=Rate.per_second(1000),
        http=HttpOptions(transport=Script().transport(), response_check=_accept_all, follow_redirects=False),
    )
    with pytest.raises(ValueError, match=r"^StubClient has a response_check=, so no view may follow redirects$"):
        client.with_options(follow_redirects=True)
    client.with_options(follow_redirects=False)


#: The messages for the override values this phase removed.
_RETRY_BOOL: Final = (
    r"^retry= takes a RetryPolicy, or None to send once \(got bool\): write retry=None for retry=False, and make the "
    r"call on the client rather than its view for retry=True$"
)
_FOLLOW_NONE: Final = r"^follow_redirects= takes a bool; leave it out to inherit \(got NoneType\)$"
_OLD_VALUES: Final[list[tuple[dict[str, Any], str]]] = [
    ({"retry": True}, _RETRY_BOOL),
    ({"retry": False}, _RETRY_BOOL),
    ({"retry": "never"}, r"^retry= takes a RetryPolicy, or None to send once \(got str\)$"),
    ({"follow_redirects": None}, _FOLLOW_NONE),
]
_OLD_VALUE_IDS: Final = ["retry-true", "retry-false", "retry-str", "follow-none"]


@pytest.mark.parametrize(("options", "message"), _OLD_VALUES, ids=_OLD_VALUE_IDS)
@pytest.mark.parametrize("method", _VERBS)
async def test_every_verb_refuses_an_old_override_value(method: Method, options: dict[str, Any], message: str) -> None:
    """Refuse ``retry=True``, ``retry=False`` and ``follow_redirects=None`` with what to write, sending nothing."""
    script = Script()
    verb = getattr(_client(script), method.lower())

    with pytest.raises(TypeError, match=message):
        await verb(_PATH, expect=nothing, **options)

    assert script.sent == []


@pytest.mark.parametrize(("options", "message"), _OLD_VALUES, ids=_OLD_VALUE_IDS)
def test_a_stream_a_walk_and_a_view_refuse_an_old_override_value(options: dict[str, Any], message: str) -> None:
    """Refuse the same values when ``stream``, ``paginate`` and ``with_options`` are called, before anything runs."""
    client = _client(Script())

    with pytest.raises(TypeError, match=message):
        client.stream(_PATH, **options)
    with pytest.raises(TypeError, match=message):
        client.paginate(_PATH, expect=nothing, pages=_ONE_PAGE, **options)
    with pytest.raises(TypeError, match=message):
        client.with_options(**options)


@pytest.mark.parametrize(
    ("ttl", "error", "message"),
    [
        (0, ValueError, r"^cache_ttl must be a positive finite number of seconds, or None \(got 0\)$"),
        (math.inf, ValueError, r"^cache_ttl must be a positive finite number of seconds, or None \(got inf\)$"),
        ("60", TypeError, r"^cache_ttl= takes seconds, or None for no lifetime limit \(got str\)$"),
    ],
    ids=["zero", "infinite", "str"],
)
async def test_a_bad_cache_ttl_is_refused(ttl: object, error: type[Exception], message: str) -> None:
    """Refuse a lifetime no entry could live by, as ``HttpCache(ttl=)`` does, sending nothing."""
    script = Script()
    options: dict[str, Any] = {"cache_ttl": ttl}

    with pytest.raises(error, match=message):
        await _client(script).get(_PATH, expect=nothing, **options)

    assert script.sent == []


async def test_an_unknown_keyword_is_refused() -> None:
    """Refuse a misspelled option on every entry point, as a function with that signature would, sending nothing."""
    script = Script()
    client = _client(script)
    typo: dict[str, Any] = {"retires": None}

    with pytest.raises(TypeError, match=r"^post\(\) got an unexpected keyword argument 'retires'$"):
        await client.post(_PATH, expect=nothing, **typo)
    with pytest.raises(TypeError, match=r"^stream\(\) got an unexpected keyword argument 'retires'$"):
        client.stream(_PATH, **typo)
    with pytest.raises(TypeError, match=r"^paginate\(\) got an unexpected keyword argument 'retires'$"):
        client.paginate(_PATH, expect=nothing, pages=_ONE_PAGE, **typo)
    with pytest.raises(TypeError, match=r"^with_options\(\) got an unexpected keyword argument 'retires'$"):
        client.with_options(**typo)
    with pytest.raises(TypeError, match=r"^stream\(\) got an unexpected keyword argument 'cache_ttl'$"):
        client.stream(_PATH, **cast("dict[str, Any]", {"cache_ttl": 60.0}))

    assert script.sent == []
