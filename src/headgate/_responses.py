# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a client call declares about the responses it can get back.

A client method is one provider endpoint, and it returns that endpoint's documented outcome as a typed value or raises
a ``ClientError``. So each call declares, where it is made, what it can get back:

* a :class:`Reader` for any 2xx, the success reader: a function of the response, wrapped as ``Reader(fn)``;
* optionally an :class:`Expect`, which adds the exact non-2xx statuses the provider documents as answers.

Anything a call did not declare raises through the client's status ladder. There is no raw mode, so no call can
receive an undeclared status. A refusal the provider writes inside a body is raised by the reader as
:class:`RefusedError`, and the client turns it into a ``ClientError`` carrying the status the wire sent.

This module is codec-free: it imports neither msgspec nor pydantic. The typed JSON ``reader`` a client builds its
success reader with comes from the flavor module it chose, :mod:`headgate.msgspec` or :mod:`headgate.pydantic`, and both
read the body as :func:`body_reader` does. :data:`as_json` reads JSON into builtins with the standard library alone.

A reader's shape errors (a :class:`~headgate.errors.DecodeError`, or a ``ValueError``, ``LookupError`` or ``TypeError``
from code over ``response.json()``, or nesting too deep to parse) become ``ResponseParseError`` on a 2xx.
"""

import codecs
import functools
import json
import math
import re
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final, NoReturn, cast, final, override

import httpx2

from headgate._core import (
    check_callable,
    check_default_headers,
    check_kind,
    check_status,
    is_model_class,
    is_type,
    named,
    sealed,
    type_text,
)
from headgate._failures import reader_misuse
from headgate._sse import EventParser, text_decoder
from headgate.errors import DecodeError, RefusedError

__all__ = [
    "Expect",
    "Reader",
    "ResponseCheck",
    "accepts",
    "as_bytes",
    "as_events",
    "as_json",
    "as_text",
    "body_reader",
    "nothing",
    "reader_hint",
    "with_answers",
]


@final
@sealed("wrap a function of the response, Reader(lambda response: ...)")
class Reader[T]:
    """Turns a response into a value: ``Reader(fn)`` around a function of the response, or ``@Reader`` over a ``def``.

    A reader receives the whole response (status, headers, body), because it may need what a header declares, such as a
    ``content-type`` choosing between JSON and an event stream. It may raise :class:`~headgate.errors.DecodeError` (the
    body is not what was declared; a ``ValueError``, ``LookupError`` or ``TypeError`` from code over
    ``response.json()``, or nesting too deep to parse, counts the same) or :class:`RefusedError` (the provider said no
    inside the body). A ``RecursionError`` it raises is read as a body nested too deeply to parse, a shape error like
    the others. It never builds a ``ClientError`` itself: the client does, so the error carries the real status
    and the client's host.

    It is a class rather than any function of a response so that ``expect=`` refuses what only looks like one: a model
    class, ``str`` and a bare function are all callable, and each is a mistake there.

    Its ``repr`` is ``Reader(<name>)``: *name* when given, else the function's ``__name__`` (a ``functools.partial``'s
    function's), else its type's.
    """

    __slots__ = ("_name", "_read")

    def __init__(self, read: Callable[[httpx2.Response], T], /, *, name: str | None = None) -> None:
        """Wrap *read*, the function that turns a response into a value, under *name* or *read*'s own.

        :raises TypeError: for a *read* that cannot be called, and for a class, here rather than at the first
            response: ``Reader(str)`` would return the response's ``repr``; and for a *name* that is not a ``str``.
        """
        if isinstance(read, type):
            fallback = f"for a class built from a response, write Reader(lambda r: {read.__name__}(r))"
            msg = f"Reader takes a function of the response (got the class {read.__name__}): "
            raise TypeError(msg + _READERS_FOR.get(read, fallback))
        if not callable(read):
            # Anything not callable is an instance, so its type's name is what _kinds would print.
            msg = f"Reader takes a function of the response (got {type(read).__name__})"
            raise TypeError(msg)
        check_kind("name", name, (str, type(None)), "the name its repr shows, a str, or None for the function's")
        # Private, so the type parameter stays covariant: a Reader[Order] is a Reader[Order | None].
        self._read = read
        self._name = _name_of(read) if name is None else name

    def __call__(self, response: httpx2.Response, /) -> T:
        """Return what the wrapped function reads from *response*."""
        return self._read(response)

    @override
    def __repr__(self) -> str:
        """Return ``Reader(<name>)``."""
        return f"Reader({self._name})"


#: The reader each builtin class someone wraps in ``Reader`` stands for.
_READERS_FOR: Final[Mapping[type, str]] = {
    str: "for the body as text, use as_text",
    bytes: "for the body as bytes, use as_bytes",
    dict: "for the body as JSON, use as_json",
    list: "for the body as JSON, use as_json",
}


def reader_hint(value: object) -> str | None:
    """Return what to write for *value* given where a reader goes, when it is a class or a type: its reader; else None.

    A model class where its reader goes is the commonest mistake there: the reader of it is a flavor's ``reader``.
    """
    if not is_type(value):
        return None
    if isinstance(value, type) and value in _READERS_FOR:
        return _READERS_FOR[value]
    return f"write reader({type_text(value)}) with a flavor's reader, from headgate.msgspec or headgate.pydantic"


def _is_trap(decode: object) -> bool:
    """Return whether *decode*, a class or a type given as a function of a body's bytes, would misread every body.

    A builtin with a reader of its own (``str`` returned the bytes' ``repr``), a model class (which takes its fields,
    not bytes) and a type expression (``list[Row]``) are; a class built from bytes (``io.BytesIO``, ``bytearray``)
    decodes the body as it is written to.
    """
    return decode in _READERS_FOR or is_model_class(decode) or not isinstance(decode, type)


def _name_of(read: object) -> str:
    """Return what a reader over *read* is named: its ``__name__``, a partial's function's, or its type's name."""
    target = read.func if isinstance(read, functools.partial) else read
    name = getattr(target, "__name__", None)
    return name if isinstance(name, str) else type(target).__name__


#: Statuses that mean "we could not ask", never an answer about the request: our own credentials (401, 407), and the
#: rate limit the retry transport and the breaker own (429). Every 5xx is refused by range beside these.
_COULD_NOT_ASK: Final = frozenset(
    {httpx2.codes.UNAUTHORIZED, httpx2.codes.PROXY_AUTHENTICATION_REQUIRED, httpx2.codes.TOO_MANY_REQUESTS}
)


def _refuse_unclaimable(status: int) -> None:
    """Raise ``ValueError`` for an HTTP status no call may declare as an answer: one that means we could not ask."""
    if status in _COULD_NOT_ASK or status >= httpx2.codes.INTERNAL_SERVER_ERROR:
        msg = f"{status} means we could not ask; it is never a documented answer"
        raise ValueError(msg)


type ResponseCheck = Callable[[httpx2.Response], RefusedError | None]
"""A response check, run on the final response before anything the call declared.

It names a refusal the status line alone cannot show, such as a key-error redirect, and returns ``None`` for
everything else; raising the :class:`RefusedError` instead of returning it is treated the same. It reads the status
line and headers, never the body: a refusal written in a body is its reader's to raise. A client carrying one does not
follow redirects (the client enforces it), because the check sees only the final response.
"""


#: The headers an ``Expect`` sends when it is given none: read-only, so one instance serves every ``Expect``.
_NO_HEADERS: Final[Mapping[str, str]] = MappingProxyType({})

#: What ``copy.replace`` may change on an ``Expect``: its answers are declared with ``on()`` only.
_REPLACEABLE: Final = frozenset({"ok", "headers", "accept"})


@final
@sealed("declare each answer with .on(status, reader)")
class Expect[T]:
    """One endpoint's documented responses: a success reader for any 2xx, plus exact-status answers.

    ``answers`` holds the statuses the provider documents as answers, each declared with :meth:`on` in the client
    method whose endpoint returns it. The client reads an exact declared status first, applies its status ladder
    to any other non-2xx, and hands every other 2xx to :attr:`ok`. A declared non-2xx whose body its reader cannot
    decode falls through to the ladder with its real status, so a claim covers only the body shape it names: a declared
    business 403 never swallows a real permission 403.

    ``Expect`` is deliberately not callable: reading a response is the client's dispatch, and a callable ``Expect``
    would be a second way to read one outside it.

    ``ok`` must be a :class:`Reader`. ``answers`` is filled only by :meth:`on`, which widens the result type with each
    answer it declares, and is a read-only mapping. An ``Expect`` is a frozen value: ``copy.replace`` (and
    ``copy.copy``) keep its answers, and since it is not a dataclass, ``dataclasses.replace``, which would drop them,
    raises ``TypeError``.

    ``headers`` are request headers the declaration needs sent (a conditional read's preconditions, see
    :func:`headgate.cache.conditional`); a header the call sets itself wins.
    ``accept`` names the media types the endpoint answers with (``"application/json"``, or a comma-separated list).
    The client sends it as the request's ``Accept`` header, unless the call sets one in ``headers=``, and checks every
    non-empty response it hands a reader against it before the reader runs. Media types compare case-insensitively
    with their parameters ignored, ``*/*`` and ``type/*`` match as in ``Accept``, and a structured-syntax suffix matches
    its base type (``application/problem+json`` is an ``application/json``). A body of another
    type raises ``ResponseParseError`` naming the status and the ``Content-Type`` on a 2xx; on a declared non-2xx it
    falls through to the status ladder, as an unreadable body does.
    """

    __slots__ = ("_accept", "_answers", "_headers", "_ok")

    def __init__(self, ok: Reader[T], *, headers: Mapping[str, str] = _NO_HEADERS, accept: str | None = None) -> None:
        """Read a 2xx with *ok*, sending *headers*, and accept *accept*'s media types; refuse a non-reader *ok*.

        :raises TypeError: for an *ok* that is not a :class:`Reader`, and an *accept* that is not a ``str``.
        :raises ValueError: for a malformed *accept*, *headers* that name a header more than once, in any case, or one
            no request can carry (a name that is not an HTTP token, a value holding CR, LF or NUL), and an *accept*
            beside an ``Accept`` in *headers*, two spellings of one header.
        """
        check_kind("ok", ok, Reader, "a Reader")
        check_default_headers("headers", headers)
        if accept is not None:
            check_kind("accept", accept, str, "media types, such as 'application/json', or None")
            _media_ranges(accept)
            if "accept" in {name.lower() for name in headers}:
                msg = (
                    "accept= and headers= both set Accept: give accept=, which each response is checked against too, "
                    "or the header alone, not both"
                )
                raise ValueError(msg)
        self._ok = ok
        self._answers: Mapping[int, Reader[T]] = MappingProxyType({})
        self._headers: Mapping[str, str] = MappingProxyType(dict(headers))
        self._accept = accept

    @property
    def ok(self) -> Reader[T]:
        """The reader of every 2xx the call does not declare as an answer."""
        return self._ok

    @property
    def answers(self) -> Mapping[int, Reader[T]]:
        """The exact statuses :meth:`on` declared, each with its reader; read-only."""
        return self._answers

    @property
    def headers(self) -> Mapping[str, str]:
        """The request headers the declaration needs sent; read-only."""
        return self._headers

    @property
    def accept(self) -> str | None:
        """The media types the endpoint answers with, sent as ``Accept`` and checked on each response."""
        return self._accept

    def on[E](self, status: int, read: Reader[E]) -> Expect[T | E]:
        """Return a new ``Expect`` that also reads *status* with *read*, widening the result type to ``T | E``.

        No ranges and no ``default``: OpenAPI allows ``4XX``, but a range is a claim about statuses nobody documented.
        This ``Expect`` is left as it was.

        :raises TypeError: for a *status* that is not an ``int`` (``"404"`` and ``True`` included), and a *read* that is
            not a :class:`Reader`.
        :raises ValueError: for a status outside 100-599; for 401, 407, 429 or any 5xx, which mean we could not ask
            (the credentials are ours, and the transport owns 429 and 5xx through retry and the breaker); or for a
            status already declared. It is raised where the ``Expect`` is built, usually at import.
        """
        check_status("status", status, "an HTTP status, an int such as HTTPStatus.NOT_FOUND")
        check_kind("read", read, Reader, "a Reader")
        _refuse_unclaimable(status)
        if status in self._answers:
            msg = f"{status} is already declared on this Expect"
            raise ValueError(msg)
        widened: Expect[T | E] = Expect(self._ok, headers=self._headers, accept=self._accept)
        answers: dict[int, Reader[T | E]] = {**self._answers, status: read}
        return with_answers(widened, answers)

    def __replace__(self, **changes: object) -> Expect[T]:
        """Return a copy with *changes* to ``ok``, ``headers`` or ``accept``, keeping the answers; for ``copy.replace``.

        :raises TypeError: for any other name, ``answers`` included, which only :meth:`on` declares.
        """
        if unknown := sorted(changes.keys() - _REPLACEABLE):
            msg = f"copy.replace() of an Expect takes ok=, headers= and accept= (got {', '.join(unknown)}=)"
            if "answers" in unknown:
                msg += ": declare an answer with .on(status, reader)"
            raise TypeError(msg)
        ok = cast("Reader[T]", changes.get("ok", self._ok))
        headers = cast("Mapping[str, str]", changes.get("headers", self._headers))
        accept = cast("str | None", changes.get("accept", self._accept))
        return with_answers(Expect(ok, headers=headers, accept=accept), self._answers)

    @override
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Expect) and self._key() == other._key()

    @override
    def __hash__(self) -> int:
        return hash(self._key())

    @override
    def __repr__(self) -> str:
        """Return the ``Expect`` as it is built: its constructor, then an ``.on()`` per answer, by status.

        Each header's value shows as ``'***'``, since it may be a credential.
        """
        built = f"Expect({self._ok!r}, headers={dict.fromkeys(self._headers, '***')!r}, accept={self._accept!r})"
        return built + "".join(f".on({status}, {read!r})" for status, read in sorted(self._answers.items()))

    def _key(self) -> tuple[object, ...]:
        """Return what two equal ``Expect`` objects share: their readers, answers, headers and accept."""
        return (self._ok, tuple(sorted(self._answers.items())), tuple(sorted(self._headers.items())), self._accept)


def with_answers[T](expect: Expect[T], answers: Mapping[int, Reader[T]]) -> Expect[T]:
    """Return *expect*, freshly built with no answers, holding *answers*, which :meth:`Expect.on` already checked."""
    # The answers are set once, here, on an Expect no one else holds yet; the class has no setter for them.
    object.__setattr__(expect, "_answers", MappingProxyType(dict(answers)))
    return expect


def accepts[T](expect: Expect[T], resp: httpx2.Response) -> bool:
    """Return whether *resp*'s ``Content-Type`` is one *expect*'s ``accept`` names; ``True`` without ``accept``.

    Media types compare case-insensitively with their parameters ignored (``application/json; charset=utf-8``
    is ``application/json``); ``*/*`` and ``type/*`` match as in ``Accept``; and a structured-syntax suffix
    (RFC 6839) matches its base type, so ``application/problem+json`` is an ``application/json``. An empty body
    (a 204, a HEAD) has nothing to check, and passes.
    """
    if expect.accept is None or not resp.content:
        return True
    found = _media_type(resp.headers.get("content-type", ""))
    if found is None:
        return False
    kind, subtype = found
    suffix = subtype.rpartition("+")[2]
    return any(
        want_kind in {"*", kind} and want_subtype in {"*", subtype, suffix}
        for want_kind, want_subtype in _media_ranges(expect.accept)
    )


#: How many distinct ``accept`` values keep their parsed form: far more than any client declares.
_PARSED_ACCEPTS: Final = 256

#: A media type's ``type/subtype``, each an RFC 9110 token, lower-cased.
_MEDIA_TYPE: Final = re.compile(r"([!#$%&'*+\-.^_`|~0-9a-z]+)/([!#$%&'*+\-.^_`|~0-9a-z]+)")


def _media_type(text: str) -> tuple[str, str] | None:
    """Return the ``(type, subtype)`` of a media type or range *text*, lower-cased and without parameters."""
    match = _MEDIA_TYPE.fullmatch(text.partition(";")[0].strip().lower())
    return None if match is None else (str(match[1]), str(match[2]))


@functools.lru_cache(maxsize=_PARSED_ACCEPTS)
def _media_ranges(accept: str) -> tuple[tuple[str, str], ...]:
    """Return the media ranges a comma-separated *accept* names; ``ValueError`` for an empty or malformed one.

    Cached, since an ``accept`` is a constant of the endpoint method, checked on every response it reads.
    """
    ranges: list[tuple[str, str]] = []
    for item in accept.split(","):
        found = _media_type(item)
        if found is None:
            msg = f"accept must name media types such as 'application/json' (got {accept!r})"
            raise ValueError(msg)
        ranges.append(found)
    return tuple(ranges)


def body_reader[T](
    decode: Callable[[bytes], T],
    /,
    *,
    empty: bytes | None = None,
    refuse: Callable[[T], RefusedError | None] | None = None,
) -> Reader[T]:
    """Build a reader that decodes the body with *decode*: for a bytes decoder in any format.

    It suits a hand-written decoder over a wire that is not JSON (an ``.xls`` workbook, a fixed-width TXT, a CSV, a
    ZIP), which its tests feed fixture files without a response. Name the reader once, at module level::

        ALLOTMENTS: Final = body_reader(decode_allotments)
        STATEMENTS: Final = body_reader(partial(decode_statements, table=Statement))

    The reader is named after *decode*, so those two print as ``Reader(decode_allotments)`` and
    ``Reader(decode_statements)``.

    A ``ValueError``, ``LookupError``, ``TypeError`` or :class:`~headgate.errors.DecodeError` from *decode*, or nesting
    too deep to parse, is a body of the wrong shape, which the client raises as
    :class:`~headgate.errors.ResponseParseError` on a 2xx.

    ``empty`` is what *decode* is handed for an empty body (zero bytes or only whitespace), such as the encoding of an
    empty array in the body's format. Left ``None``, an empty body is handed over as it is.

    ``refuse`` runs on the decoded value and returns a :class:`RefusedError` for a refusal written inside a 2xx
    body, or ``None``; the reader raises what it returns. Anything else it returns is a ``TypeError`` naming
    ``refuse=``, raised as it is, never read as a body of the wrong shape. Type a check over a small structural
    ``Protocol`` naming only the envelope fields it reads. Have it refuse only codes every consumer of the endpoint
    would read as failure (auth, quota, rate limit, server, a malformed request, any undocumented code) and leave a
    code about the subject (an unknown instrument, a rejected order) in the value for the caller.

    :raises TypeError: for a *decode* or a ``refuse`` that cannot be called, an ``empty`` that is not ``bytes``, and a
        *decode* that is a class or a type, saying which reader to write: ``body_reader(str)`` would return the body's
        ``repr``, and a model class would hold the bytes.
    """
    return _body_reader(decode, empty=empty, refuse=refuse, name=_name_of(decode))


def _body_reader[T](
    decode: Callable[[bytes], T],
    /,
    *,
    empty: bytes | None,
    refuse: Callable[[T], RefusedError | None] | None,
    name: str,
) -> Reader[T]:
    """Build the reader :func:`body_reader` documents, named *name*: for a builder that names its reader itself.

    :raises TypeError: as :func:`body_reader` documents.
    """
    check_callable("body_reader(decode)", decode, "a function of the body's bytes")
    if (hint := reader_hint(decode)) is not None and _is_trap(decode):
        if decode is bytes and refuse is not None:
            # as_bytes takes no refuse=, so the bytes are read with a function that hands them on as they are.
            hint = "write body_reader(lambda content: content, refuse=...) to check the body's bytes"
        msg = f"body_reader(decode) takes a function of the body's bytes (got {named(decode)}): {hint}"
        raise TypeError(msg)
    check_kind("empty", empty, (bytes, type(None)), "the bytes an empty body is read as, or None")
    if refuse is not None:
        check_callable(
            "refuse", refuse, "a function of the decoded value, returning a RefusedError or None to accept it"
        )

    def _read(resp: httpx2.Response, /) -> T:
        content = resp.content
        if empty is not None and not content.strip():
            content = empty
        value = decode(content)
        if refuse is not None and (refused := refuse(value)) is not None:
            if not isinstance(refused, RefusedError):
                msg = f"refuse= returns a RefusedError, or None to accept the value (got {named(refused)})"
                raise reader_misuse(msg)
            raise refused
        return value

    return Reader(_read, name=name)


def _as_bytes(resp: httpx2.Response, /) -> bytes:
    return resp.content


#: Return the body undecoded, for a wire that is not a format from the client's point of view.
#:
#: A CSV, TXT, ZIP, PDF or XML dump a parser reads later, or an opaque blob. It never raises. It is named ``as_bytes``
#: rather than ``body`` because ``body`` is the commonest parameter name in a client and would shadow the import.
as_bytes: Final[Reader[bytes]] = Reader(_as_bytes, name="as_bytes")


def _as_text(resp: httpx2.Response, /) -> str:
    content = resp.content
    if content.startswith(codecs.BOM_UTF8):
        return content.removeprefix(codecs.BOM_UTF8).decode("utf-8", "replace")
    return text_decoder(resp.encoding).decode(content, final=True)


#: Return the body as text, decoded by the response's ``charset``, as ``Stream.aiter_lines`` decodes it.
#:
#: A plain-text, CSV or HTML body a parser of text reads later. A leading UTF-8 byte order mark is dropped, and the
#: rest read as UTF-8, which the mark says it is, whatever the ``charset``. A ``charset`` that names no text encoding
#: (``base64``, ``rot13``) is read as UTF-8, as no ``charset`` is, where ``response.text`` raises, and bytes the charset
#: decodes badly become replacement characters. A body the ``charset`` cannot decode at all (UTF-16 without a byte
#: order mark) raises ``UnicodeDecodeError``, which the client reports as ``ResponseParseError``.
as_text: Final[Reader[str]] = Reader(_as_text, name="as_text")


#: What :data:`as_json` says of a body nested too deeply for ``json.loads``'s own stack.
_NESTED_TOO_DEEPLY: Final = "not JSON: nested too deeply"

#: What :data:`as_json` says of ``NaN``, ``Infinity``, a number too large for a float, or an integer too long to read.
_NOT_A_NUMBER: Final = "not JSON: NaN, Infinity or a number out of range"

#: A JSON escape of a UTF-16 surrogate: only text holding one can decode to a lone surrogate.
_SURROGATE_ESCAPE: Final = re.compile(r"\\u[dD][89a-fA-F]")

#: A UTF-16 surrogate, which a decoded string holds only from an escape that no pair completed.
_SURROGATE: Final = re.compile("[\ud800-\udfff]")


def _refuse_constant(name: str, /) -> NoReturn:
    """Refuse ``NaN``, ``Infinity`` and ``-Infinity``, which RFC 8259 leaves out of JSON."""
    raise ValueError(name)


def _finite(text: str, /) -> float:
    """Read a JSON number with a fraction or exponent, refusing one too large for a float (``1e400`` is ``inf``)."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(text)
    return value


def _holds_a_lone_surrogate(value: object) -> bool:
    """Return whether any string or key in *value* holds a surrogate, walking an explicit stack, never recursing."""
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            if _SURROGATE.search(node):
                return True
        elif isinstance(node, dict):
            pending.extend(node.keys())
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return False


def _decode_json(content: bytes, /) -> object:
    """Decode *content* as strict RFC 8259 JSON in UTF-8, raising :class:`DecodeError` that names no input.

    No library error is chained or quoted: ``UnicodeDecodeError``'s message quotes the byte it refused.
    """
    try:
        text = content.decode("utf-8")
        value = json.loads(text, parse_constant=_refuse_constant, parse_float=_finite)
    except UnicodeDecodeError as exc:
        msg = f"not UTF-8 at byte {exc.start}"
        raise DecodeError(msg) from None
    except json.JSONDecodeError as exc:
        msg = f"not JSON at line {exc.lineno}, column {exc.colno}"
        raise DecodeError(msg) from None
    except ValueError:
        raise DecodeError(_NOT_A_NUMBER) from None
    except RecursionError:
        raise DecodeError(_NESTED_TOO_DEEPLY) from None
    if _SURROGATE_ESCAPE.search(text) and _holds_a_lone_surrogate(value):
        msg = "not JSON: a string with a lone surrogate"
        raise DecodeError(msg) from None
    return value


#: Return the body as JSON read into builtins (``dict``, ``list``, ``str``, ``int``, ``float``, ``bool``, ``None``) by
#: the standard library's ``json.loads``: JSON without a codec, for a core-only install.
#:
#: It is typed ``object``, the honest type of ``json.loads``: narrow it with ``isinstance`` or a ``match``. It reads
#: strict RFC 8259 JSON, as ``headgate.msgspec``'s reader does: the body must be UTF-8 (no BOM, no UTF-16 or UTF-32),
#: and ``NaN``, ``Infinity``, a number too large for a float, an integer of over 4300 digits and a string holding a lone
#: surrogate are refused. Each refusal is a :class:`~headgate.errors.DecodeError` naming the position, never the input,
#: so a call raises ``ResponseParseError`` on a 2xx.
as_json: Final[Reader[object]] = _body_reader(_decode_json, empty=None, refuse=None, name="as_json")


def _nothing(resp: httpx2.Response, /) -> None:
    del resp


#: Return ``None``: the call's answer is that it completed. For a void write (a cancel, a delete, a reset) or HEAD.
#:
#: Throwing a body away is a claim about the wire, so declare ``nothing`` only for an endpoint documented to send no
#: body you need. Await such a call as a statement: type checkers reject binding a ``None``-typed result.
nothing: Final[Reader[None]] = Reader(_nothing, name="nothing")


def _as_events(resp: httpx2.Response, /) -> list[httpx2.ServerSentEvent]:
    return list(EventParser().feed(resp.content))


#: Return each server-sent event in the buffered body, in order, as ``Stream.aiter_events`` yields them.
#:
#: It is the same WHATWG parser: each event carries its ``event``, ``data``, ``id`` and ``retry``, the body is UTF-8
#: whatever its ``charset`` says, and a block without data (a ``retry:`` hint, an ``event: ping``) is not an event.
#: No per-event limit applies, because the body is already buffered and capped by the client, and a large reply is
#: legitimately one event. Choosing whether a body is an event stream is the caller's job, usually by reading the
#: ``content-type``.
as_events: Final[Reader[list[httpx2.ServerSentEvent]]] = Reader(_as_events, name="as_events")
