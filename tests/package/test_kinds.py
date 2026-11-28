# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A value of the wrong kind is refused where it is written, by every parameter, naming the option; values are frozen.

Every constructor and function the public modules export is called once per parameter with a value of no kind any
parameter takes, its other required parameters given valid values, and must raise ``TypeError`` or ``ValueError``
whose message names the parameter: an export or a parameter added without a check fails here. What the call returns is
used as a caller writes it (awaited, or entered as a context manager), and no further: an iterator is not iterated. The
methods a caller configures with are listed by hand. A parameter that takes any value is allowed, with its reason.

Every exported dataclass is frozen, so a value never changes under what holds it.
"""

from __future__ import annotations

import annotationlib
import contextlib
import dataclasses
import functools
import importlib
import inspect
import ipaddress
from typing import TYPE_CHECKING, Any, Final, override

import httpx2
import pytest
from hishel import AsyncSqliteStorage

import headgate.msgspec
from headgate import CachedTokenAuth, Client, Expect, HttpOptions, Minted, Paginated, Rate, Timeouts, nothing
from headgate.breaker import BreakerRegistry
from headgate.cache import Validator
from headgate.pagination import Offset, PageRequest
from headgate.testing import FakeNetwork, FakeTransport, Respond, TestClock, fake_network
from headgate.timing import Timing
from tests.package.test_api_surface import _PUBLIC_MODULES

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Mapping


class _Wrong:
    """A value of no kind any parameter takes."""

    @override
    def __repr__(self) -> str:
        return "<Wrong>"


#: The wrong value every case passes: typed ``Any``, as a caller's mistake reaches the code.
_WRONG: Final[Any] = _Wrong()

#: A reader of a JSON object, for every parameter that takes a reader.
_READ: Final = headgate.msgspec.reader(dict[str, object])


def _hook(*args: object) -> None:
    """Stand for a function a value holds and no check calls: a pager's hook, a decoder, a walk's pages."""
    del args


#: A valid value for each parameter an export requires, built afresh for each call.
_VALID: Final[Mapping[str, Callable[[], object]]] = {
    "address": lambda: ipaddress.ip_address("8.8.8.8"),
    "after_bytes": lambda: 10,
    "base_url": lambda: "https://api.example.test/",
    "client": lambda: Client(base_url="https://api.example.test/"),
    "codec": lambda: headgate.msgspec.CODEC,
    "content": lambda: b"{}",
    "decode": lambda: _hook,
    "fields": lambda: {"a": "b"},
    "files": lambda: {"f": b"data"},
    "fragment": lambda: "/x",
    "headers": lambda: {"X-Key": "v"},
    "impersonate": lambda: "chrome",
    "items": lambda: _hook,
    "items_of": lambda: _hook,
    "lifetime": lambda: 60.0,
    "more": lambda: _hook,
    "next": lambda: _hook,
    "offset": lambda: _hook,
    "ok": lambda: _READ,
    "page_size": lambda: 10,
    "pages": lambda: _hook,
    "param": lambda: "after",
    "params": lambda: {"key": "v"},
    "period": lambda: 1.0,
    "put": lambda: _hook,
    "rate": lambda: Rate(10, 1.0),
    "read": lambda: _READ,
    "reader": lambda: _READ,
    "registry": BreakerRegistry,
    "requests": lambda: 10,
    "schedule": lambda: [Respond()],
    "seconds": lambda: 1.0,
    "storage": lambda: AsyncSqliteStorage(database_path=":memory:"),
    "timing": Timing,
    "token": lambda: "t0k3n",
    "transport": FakeTransport,
    "ttl": lambda: 60.0,
    "type_": lambda: dict,
    "url": lambda: "x",
    "validator": lambda: Validator(etag='"x"'),
    "value": lambda: {"a": 1},
}

#: Exports a caller reads but does not configure with: the readers a client calls with a response, the view a
#: middleware reads of the request it was handed, and the records of a problem, a breaker and a send.
_HANDED_OUT: Final = frozenset(
    {
        "headgate.as_bytes",
        "headgate.as_events",
        "headgate.as_json",
        "headgate.as_text",
        "headgate.nothing",
        "headgate.call_view",
        "headgate.errors.Problem",
        "headgate.health.BreakerSnapshot",
        "headgate.testing.FaultCall",
    }
)

#: Parameters that take any value, each with the reason.
_ANY_VALUE: Final[Mapping[str, str]] = {
    "headgate.Minted(token=)": "the auth's own token, of whatever type its mint returns",
    "headgate.cache.Fresh(value=)": "the value the wrapped reader read, of whatever type it reads",
    "headgate.pagination.PageRequest(payload=)": "the caller's own body value, which the pager's writer encodes",
    "headgate.pagination.PageRequest.with_payload(payload=)": "the caller's own body value, as PageRequest(payload=)",
    "headgate.msgspec.to_builtins(value=)": "any value msgspec converts; its TypeError names a type it cannot",
    "headgate.pydantic.to_builtins(value=)": "any value pydantic converts; its TypeError names a type it cannot",
}

#: Refusals that name the option by something other than the parameter's name: a constructor's one argument by its
#: class. A type argument (a ``TypeForm`` parameter) is refused in the library's words, which name the type it got.
_NAMED_BY: Final[Mapping[str, str]] = {"headgate.Raw(content=)": "Raw", "headgate.Reader(read=)": "Reader"}

#: The methods' parameters a caller cannot write as a keyword, each named as its call reads: a positional-only one, and
#: the replies a route takes one after another. A constructor's or a function's are named so without a listing here.
_AS_CALLED: Final[Mapping[str, str]] = {
    "headgate.testing.FakeTransport.reply(replies=)": "reply(*replies)",
    "headgate.testing.TestClock.sleep(seconds=)": "sleep(seconds)",
}


def _call(export: Callable[..., object], signature: inspect.Signature, target: str) -> object:
    """Call *export* with the wrong value for *target*, and a valid value for each other parameter it requires."""
    args: list[object] = []
    kwargs: dict[str, object] = {}
    for parameter in signature.parameters.values():
        if parameter.name == target:
            value: object = _WRONG
        elif parameter.default is not parameter.empty:
            value = parameter.default
        else:
            value = _VALID[parameter.name]()
        if parameter.kind is parameter.POSITIONAL_ONLY:
            args.append(value)
        elif parameter.name == target or parameter.default is parameter.empty:
            kwargs[parameter.name] = value
    return export(*args, **kwargs)


def _exports() -> Iterator[tuple[str, object]]:
    """Yield each export of the public modules, with its dotted name."""
    for module_name in _PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        for name in module.__all__:
            yield f"{module_name}.{name}", getattr(module, name)


def _parameter_cases() -> Iterator[tuple[str, Callable[[], object], str]]:
    """Yield a case per parameter of each export a caller configures with: where it is, its call, what names it.

    The events are left out, which the library sends, and so are the errors it raises, and every export with no
    signature (a ``TypedDict`` of options).
    """
    for name, export in _exports():
        if name.startswith("headgate.events.") or name in _HANDED_OUT or not callable(export):
            continue
        if isinstance(export, type) and issubclass(export, BaseException):
            continue
        try:
            signature = inspect.signature(export, annotation_format=annotationlib.Format.STRING)
        except TypeError, ValueError:
            continue
        signature = signature.replace(
            parameters=[p for p in signature.parameters.values() if p.kind not in {p.VAR_POSITIONAL, p.VAR_KEYWORD}]
        )
        for parameter in signature.parameters.values():
            where = f"{name}({parameter.name}=)"
            typed = "TypeForm" in str(parameter.annotation)
            # A positional-only parameter is named as its call reads, Route(fragment): fragment= is no keyword of it.
            positional = f"{name.rpartition('.')[2]}({parameter.name})"
            plain = positional if parameter.kind is parameter.POSITIONAL_ONLY else parameter.name
            named = _NAMED_BY.get(where, repr(_WRONG) if typed else plain)
            yield where, functools.partial(_call, export, signature, parameter.name), named


def _client() -> Client:
    """Build a client on a fake network that answers ``GET /x``, and streams an event on ``GET /s``."""
    events = Respond(headers={"Content-Type": "text/event-stream"}, content=b"data: a\n\n")
    fake = FakeTransport().reply("GET", "/x", 200).reply("GET", "/s", events)
    return Client(base_url="https://api.example.test/", http=HttpOptions(transport=fake))


async def _on_stream(method: str, option: str) -> None:
    """Call *method* of an open stream with the wrong value for *option*, and leave the iterator it returns unread."""
    async with _client().stream("s") as stream:
        getattr(stream, method)(**{option: _WRONG})


async def _pages() -> AsyncIterator[list[int]]:
    """Yield one page of one item."""
    yield [1]


def _items(page: list[int]) -> list[int]:
    """Return the items on *page*, a page that is its items."""
    return page


#: A page walk's pager, for the calls that walk.
_PAGER: Final = Offset(headgate.msgspec.reader(list[int]), items=_items, page_size=1)


def _network() -> FakeNetwork[FakeTransport]:
    """Return the handle of a fake network, for the calls a caller makes on one."""
    with fake_network(FakeTransport()) as net:
        return net


class _Minting(CachedTokenAuth[str]):
    """An auth whose mint makes no request, for the calls a caller makes on an auth."""

    @override
    async def mint(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        yield Minted("t0k3n", 60.0)


#: The methods a caller configures with, each called with the wrong value for the option its name ends with.
_METHODS: Final[Mapping[str, Callable[[], object]]] = {
    "headgate.Expect.on(status=)": lambda: Expect(_READ).on(_WRONG, nothing),
    "headgate.Expect.on(read=)": lambda: Expect(_READ).on(404, _WRONG),
    "headgate.Paginated.collect(max_items=)": lambda: Paginated(_pages, _items).collect(max_items=_WRONG),
    "headgate.Rate.per_second(requests=)": lambda: Rate.per_second(_WRONG),
    "headgate.Rate.per_minute(requests=)": lambda: Rate.per_minute(_WRONG),
    "headgate.Timeouts.over(base=)": lambda: Timeouts().over(_WRONG),
    "headgate.Client.get(url=)": lambda: _client().get(_WRONG, expect=nothing),
    "headgate.Client.get(expect=)": lambda: _client().get("x", expect=_WRONG),
    "headgate.Client.get(timeout=)": lambda: _client().get("x", expect=nothing, timeout=_WRONG),
    "headgate.Client.get(deadline=)": lambda: _client().get("x", expect=nothing, deadline=_WRONG),
    "headgate.Client.get(cache_ttl=)": lambda: _client().get("x", expect=nothing, cache_ttl=_WRONG),
    "headgate.Client.post(url=)": lambda: _client().post(_WRONG, expect=nothing),
    "headgate.Client.post(expect=)": lambda: _client().post("x", expect=_WRONG),
    "headgate.Client.post(body=)": lambda: _client().post("x", expect=nothing, body=_WRONG),
    "headgate.Client.query(url=)": lambda: _client().query(_WRONG, expect=nothing),
    "headgate.Client.query(expect=)": lambda: _client().query("x", expect=_WRONG),
    "headgate.Client.query(body=)": lambda: _client().query("x", expect=nothing, body=_WRONG),
    "headgate.Client.put(url=)": lambda: _client().put(_WRONG, expect=nothing),
    "headgate.Client.put(expect=)": lambda: _client().put("x", expect=_WRONG),
    "headgate.Client.put(body=)": lambda: _client().put("x", expect=nothing, body=_WRONG),
    "headgate.Client.patch(url=)": lambda: _client().patch(_WRONG, expect=nothing),
    "headgate.Client.patch(expect=)": lambda: _client().patch("x", expect=_WRONG),
    "headgate.Client.patch(body=)": lambda: _client().patch("x", expect=nothing, body=_WRONG),
    "headgate.Client.delete(url=)": lambda: _client().delete(_WRONG, expect=nothing),
    "headgate.Client.delete(expect=)": lambda: _client().delete("x", expect=_WRONG),
    "headgate.Client.delete(body=)": lambda: _client().delete("x", expect=nothing, body=_WRONG),
    "headgate.Client.head(url=)": lambda: _client().head(_WRONG, expect=nothing),
    "headgate.Client.head(expect=)": lambda: _client().head("x", expect=_WRONG),
    "headgate.Client.head(timeout=)": lambda: _client().head("x", expect=nothing, timeout=_WRONG),
    "headgate.Client.events(url=)": lambda: _client().events(_WRONG),
    "headgate.Client.events(last_event_id=)": lambda: _client().events("s", last_event_id=_WRONG),
    "headgate.Client.events(headers=)": lambda: _client().events("s", headers=_WRONG),
    "headgate.Client.events(timeout=)": lambda: _client().events("s", timeout=_WRONG),
    "headgate.Client.paginate(pager=)": lambda: _client().paginate("x", pager=_WRONG),
    "headgate.Client.paginate(max_pages=)": lambda: _client().paginate("x", pager=_PAGER, max_pages=_WRONG),
    "headgate.Client.paginate(on_limit=)": lambda: _client().paginate("x", pager=_PAGER, on_limit=_WRONG),
    "headgate.Client.stream(url=)": lambda: _client().stream(_WRONG),
    "headgate.Client.with_options(headers=)": lambda: _client().with_options(headers=_WRONG),
    "headgate.Client.with_options(timeout=)": lambda: _client().with_options(timeout=_WRONG),
    "headgate.Client.with_options(retry=)": lambda: _client().with_options(retry=_WRONG),
    "headgate.Client.with_options(deadline=)": lambda: _client().with_options(deadline=_WRONG),
    "headgate.CachedTokenAuth.seed(minted=)": lambda: _Minting().seed(_WRONG),
    "headgate.CachedTokenAuth.reject_credential_rotation(returned=)": lambda: _Minting().reject_credential_rotation(
        _WRONG, "k3y", setting="API_KEY"
    ),
    "headgate.CachedTokenAuth.reject_credential_rotation(configured=)": lambda: _Minting().reject_credential_rotation(
        "k3y", _WRONG, setting="API_KEY"
    ),
    "headgate.cache.Validator.of(response=)": lambda: Validator.of(_WRONG),
    "headgate.cache.Validator.of(held=)": lambda: Validator.of(httpx2.Response(200), held=_WRONG),
    "headgate.pagination.PageRequest.with_param(name=)": lambda: PageRequest("x").with_param(_WRONG, 1),
    "headgate.pagination.PageRequest.with_payload(payload=)": lambda: PageRequest("x").with_payload(_WRONG),
    "headgate.streaming.Stream.aiter_bytes(chunk_size=)": lambda: _on_stream("aiter_bytes", "chunk_size"),
    "headgate.streaming.Stream.aiter_lines(max_line_size=)": lambda: _on_stream("aiter_lines", "max_line_size"),
    "headgate.streaming.Stream.aiter_events(max_event_size=)": lambda: _on_stream("aiter_events", "max_event_size"),
    "headgate.testing.FakeTransport.reply(method=)": lambda: FakeTransport().reply(_WRONG, "/x", 200),
    "headgate.testing.FakeTransport.reply(path=)": lambda: FakeTransport().reply("GET", _WRONG, 200),
    "headgate.testing.FakeTransport.reply(replies=)": lambda: FakeTransport().reply("GET", "/x", _WRONG),
    "headgate.testing.FakeTransport.calls_to(method=)": lambda: FakeTransport().calls_to(_WRONG, "/x"),
    "headgate.testing.FakeTransport.calls_to(path=)": lambda: FakeTransport().calls_to("GET", _WRONG),
    "headgate.testing.FakeNetwork.resolve(host=)": lambda: _network().resolve(_WRONG),
    "headgate.testing.FakeNetwork.resolve(addresses=)": lambda: _network().resolve("api.test", _WRONG),
    "headgate.testing.TestClock.advance(seconds=)": lambda: TestClock().advance(_WRONG),
    "headgate.testing.TestClock.sleep(seconds=)": lambda: TestClock().sleep(_WRONG),
}


def _method_cases() -> Iterator[tuple[str, Callable[[], object], str]]:
    """Yield a case per method option in :data:`_METHODS`: where it is, its call, and the option it names."""
    for where, call in _METHODS.items():
        yield where, call, _AS_CALLED.get(where, where.rpartition("(")[2].removesuffix("=)"))


#: Every case, those of the parameters that take any value included.
_EVERY_CASE: Final = [*_parameter_cases(), *_method_cases()]

#: The cases that must refuse the wrong value.
_CASES: Final = [case for case in _EVERY_CASE if case[0] not in _ANY_VALUE]


async def _as_written(result: object) -> None:
    """Use *result* as the code that made the call goes on: await it, or enter and leave it."""
    if inspect.isawaitable(result):
        await result
    elif isinstance(result, contextlib.AbstractAsyncContextManager):
        async with result:
            pass
    elif isinstance(result, contextlib.AbstractContextManager):
        with result:
            pass


@pytest.mark.parametrize(("where", "call", "named"), _CASES, ids=[where for where, _, _ in _CASES])
async def test_a_wrong_kind_is_refused_where_it_is_written(where: str, call: Callable[[], object], named: str) -> None:
    """Refuse a value of the wrong kind with a ``TypeError`` or ``ValueError`` that names the option."""
    with pytest.raises((TypeError, ValueError)) as refused:
        await _as_written(call())

    assert named.lower() in str(refused.value).lower(), f"{where} raised {refused.value!r}"


def test_the_allowlists_name_what_there_is() -> None:
    """Keep the allowlists to options and exports that exist, so a renamed one is not passed over unseen."""
    every = {where for where, _, _ in _EVERY_CASE}

    assert sorted(_ANY_VALUE.keys() - every) == []
    assert sorted(_NAMED_BY.keys() - every) == []
    assert sorted(_AS_CALLED.keys() - every) == []
    assert sorted(_HANDED_OUT - {name for name, _ in _exports()}) == []


def test_every_exported_dataclass_is_frozen() -> None:
    """Freeze every exported dataclass: a value a caller built, or the library handed out, never changes under it."""
    thawed = [
        name
        for name, export in _exports()
        if isinstance(export, type)
        and dataclasses.is_dataclass(export)
        and not vars(export)["__dataclass_params__"].frozen
    ]

    assert thawed == []
