# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a client call declares about the responses it can get back.

A client method is one provider endpoint, and it returns that endpoint's documented outcome as a typed value or raises
a ``ClientError``. So each call declares, where it is made, what it can get back:

* a :data:`Reader` for any 2xx, the success reader;
* optionally an :class:`Expect`, which adds the exact non-2xx statuses the provider documents as answers.

Anything a call did not declare raises through the base client's status ladder. There is no raw mode, so no call can
receive an undeclared status. A refusal the provider writes inside a body is raised by the reader as
:class:`RefusedError`, and the base client turns it into a ``ClientError`` carrying the status the wire sent.

This module is codec-free: it imports neither msgspec nor pydantic. The JSON ``decoder`` a client builds its success
reader with comes from the flavor module it chose, :mod:`headgate.msgspec` or :mod:`headgate.pydantic`, and both build
on :func:`json_reader`.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, final

import httpx2

from headgate.errors import UpstreamStatusError

__all__ = [
    "Expect",
    "Reader",
    "RefusalCheck",
    "RefusedError",
    "accepts",
    "as_bytes",
    "event_stream",
    "json_reader",
    "nothing",
    "reader_over",
]


type Reader[T] = Callable[[httpx2.Response], T]
"""Turns a response into a value.

A reader receives the whole response (status, headers, body), because it may need what a header declares, such as a
``content-type`` choosing between JSON and an event stream. It may raise :class:`~headgate.errors.DecodeError` (the
body is not what was declared; a ``ValueError``, ``LookupError`` or ``TypeError`` from code over ``response.json()``
counts the same) or :class:`RefusedError` (the provider said no inside the body). It never builds a ``ClientError``
itself: the base client does, so the error carries the real status and the client's name.
"""

#: Statuses that mean "we could not ask", never an answer about the request: our own credentials (401, 407), and the
#: rate limit the retry transport and the breaker own (429). Every 5xx is refused by range beside these.
_COULD_NOT_ASK: Final = frozenset(
    {httpx2.codes.UNAUTHORIZED, httpx2.codes.PROXY_AUTHENTICATION_REQUIRED, httpx2.codes.TOO_MANY_REQUESTS}
)

#: The status range RFC 9110 defines.
_MIN_STATUS, _MAX_STATUS = 100, 599


def _refuse_unclaimable(status: int) -> None:
    """Raise ``ValueError`` for a status no call may declare as an answer.

    Both ways of declaring an answer run this one check (the :class:`Expect` constructor over every key of
    ``answers``, and :meth:`Expect.on`), so they refuse the same statuses with the same message.
    """
    if not _MIN_STATUS <= status <= _MAX_STATUS:
        msg = f"{status} is not an HTTP status"
        raise ValueError(msg)
    if status in _COULD_NOT_ASK or status >= httpx2.codes.INTERNAL_SERVER_ERROR:
        msg = f"{status} means we could not ask; it is never a documented answer"
        raise ValueError(msg)


class RefusedError(Exception):
    """A provider's refusal, as a reader or a host check saw it.

    A reader raises it for a refusal written inside a body (an error envelope, a ``REQUEST_NOT_PROCESSED`` status, a
    vendor error code); a ``decoder``'s and a client's ``refusal_check`` return one instead. The base client is
    its only catcher, and turns it into ``kind(message, status_code=<the status the wire sent>, code=code, ...)``, so
    a refusal in a 200 body carries 200.

    ``code`` is the provider's own code. ``kind`` is the ``ClientError`` class that code means: ``AuthError`` for a
    credential, ``RateLimitError`` for a quota or rate limit, and ``UpstreamStatusError`` otherwise.

    It is an exception rather than a return value because ``T | RefusedError`` would widen every reader's type and
    blur a refusal with the values a reader legitimately returns. It is not a ``ClientError``: the base client
    converts it before a caller of the client sees it.
    """

    def __init__(
        self, message: str, *, code: str | None = None, kind: type[UpstreamStatusError] = UpstreamStatusError
    ) -> None:
        """Record the refusal's *message*, the provider's *code*, and the ``ClientError`` *kind* it becomes."""
        super().__init__(message)
        self.message = message
        self.code = code
        self.kind = kind


type RefusalCheck = Callable[[httpx2.Response], RefusedError | None]
"""A per-host check, run on the final response before anything the call declared.

It names a refusal the status line alone cannot show, such as a key-error redirect, and returns ``None`` for
everything else; raising the :class:`RefusedError` instead of returning it is treated the same. It reads the status
line and headers, never the body: a refusal written in a body is its reader's to raise. A client carrying one does not
follow redirects (the base client enforces it), because the check sees only the final response.
"""


@final
@dataclass(frozen=True, slots=True)
class Expect[T]:
    """One endpoint's documented responses: a success reader for any 2xx, plus exact-status answers.

    ``answers`` holds the statuses the provider documents as answers, each declared with :meth:`on` in the client
    method whose endpoint returns it. The base client reads an exact declared status first, applies its status ladder
    to any other non-2xx, and hands every other 2xx to :attr:`ok`. A declared non-2xx whose body its reader cannot
    decode falls through to the ladder with its real status, so a claim covers only the body shape it names: a declared
    business 403 never swallows a real permission 403.

    ``Expect`` is deliberately not callable. A verb takes ``Reader[T] | Expect[T]``, and that union is unambiguous only
    because of it.

    The constructor refuses exactly what :meth:`on` refuses, since an ``Expect`` built with ``answers`` makes the same
    claim, and then freezes ``answers`` into a read-only copy that neither the caller's dict nor the default can change.

    ``headers`` are request headers the declaration needs sent (a conditional read's preconditions, see
    :func:`headgate.cache.conditional`); a header the call sets itself wins.
    ``accept`` names the media types the endpoint answers with (``"application/json"``, or a comma-separated list).
    The client sends it as the request's ``Accept`` header, unless the call sets one in ``headers=``, and checks every
    non-empty response it hands a reader against it before the reader runs (see :func:`accepts`). A body of another
    type raises ``ResponseParseError`` naming the status and the ``Content-Type`` on a 2xx; on a declared non-2xx it
    falls through to the status ladder, as an unreadable body does.
    """

    ok: Reader[T]
    answers: Mapping[int, Reader[T]] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict, kw_only=True)
    accept: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Refuse an unclaimable status in ``answers`` or a malformed ``accept``; freeze ``answers`` and ``headers``."""
        for status in self.answers:
            _refuse_unclaimable(status)
        # A frozen, slotted dataclass refuses plain assignment even here; object.__setattr__ is the documented route.
        object.__setattr__(self, "answers", MappingProxyType(dict(self.answers)))
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        if self.accept is not None:
            _media_ranges(self.accept)

    def on[E](self, status: int, read: Reader[E]) -> Expect[T | E]:
        """Return a new ``Expect`` that also reads *status* with *read*, widening the result type to ``T | E``.

        No ranges and no ``default``: OpenAPI allows ``4XX``, but a range is a claim about statuses nobody documented.
        This ``Expect`` is left as it was.

        :raises ValueError: for a status outside 100-599; for 401, 407, 429 or any 5xx, which mean we could not ask
            (the credentials are ours, and the transport owns 429 and 5xx through retry and the breaker); or for a
            status already declared. It is raised where the ``Expect`` is built, usually at import.
        """
        _refuse_unclaimable(status)
        if status in self.answers:
            msg = f"{status} is already declared on this Expect"
            raise ValueError(msg)
        widened: dict[int, Reader[T | E]] = {**self.answers, status: read}
        return Expect(self.ok, widened, headers=self.headers, accept=self.accept)


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


def json_reader[T](
    decode: Callable[[bytes], T],
    *,
    allow_empty_body: bool = False,
    refusal_check: Callable[[T], RefusedError | None] | None = None,
) -> Reader[T]:
    """Build a reader around a JSON bytes-to-value function, for a decoder that is not a :class:`~headgate.Codec`.

    :func:`~headgate.decoder_for`, which every codec's ``decoder`` builds on, shares this reader's empty-body rule and
    ``refusal_check`` hook, so they behave identically whichever builds the reader.

    ``allow_empty_body`` reads an empty body (zero bytes or only whitespace) as the empty array ``[]``. It suits only a
    list-shaped type, for a provider whose no-data answer is a zero-byte 200 or a 204. For every other provider an empty
    200 is corruption, and decoding it fails.

    ``refusal_check`` runs on the decoded value and returns a :class:`RefusedError` for a refusal written inside a 2xx
    body, or ``None``; the reader raises what it returns. Type a check over a small structural ``Protocol`` naming only
    the envelope fields it reads. Have it refuse only codes every consumer of the endpoint would read as failure (auth,
    quota, rate limit, server, a malformed request, any undocumented code) and leave a code about the subject (an
    unknown instrument, a rejected order) in the value for the caller.
    """
    return reader_over(decode, empty=b"[]" if allow_empty_body else None, refusal_check=refusal_check)


def reader_over[T](
    decode: Callable[[bytes], T],
    *,
    empty: bytes | None,
    refusal_check: Callable[[T], RefusedError | None] | None,
) -> Reader[T]:
    """Build a reader that decodes the body with *decode*, reading an empty body as *empty* when it is given.

    An empty body is zero bytes or only whitespace. *empty* is the encoding of an empty array in the body's format
    (``b"[]"`` for JSON), or ``None`` to decode an empty body as it is, which fails for every format worth declaring.
    """

    def _read(resp: httpx2.Response, /) -> T:
        content = resp.content
        if empty is not None and not content.strip():
            content = empty
        value = decode(content)
        if refusal_check is not None and (refused := refusal_check(value)) is not None:
            raise refused
        return value

    return _read


def as_bytes(resp: httpx2.Response, /) -> bytes:
    """Return the body undecoded, for a wire that is not a format from the client's point of view.

    A CSV, TXT, ZIP, PDF or XML dump a parser reads later, or an opaque blob. It never raises. It is named ``as_bytes``
    rather than ``body`` because ``body`` is the commonest parameter name in a client and would shadow the import.
    """
    return resp.content


def nothing(resp: httpx2.Response, /) -> None:
    """Return ``None``: the call's answer is that it completed. For a void write (a cancel, a delete, a reset) or HEAD.

    Throwing a body away is a claim about the wire, so declare ``nothing`` only for an endpoint documented to send no
    body you need. Await such a call as a statement: type checkers reject binding a ``None``-typed result.
    """
    del resp


#: A reserved (RFC 2606) URL for the request an ``httpx2.Response`` must carry. Nothing is sent to it.
_UNSENT_URL: Final = "https://sse.invalid/"


def event_stream(resp: httpx2.Response, /) -> list[httpx2.ServerSentEvent]:
    """Return each server-sent event in the buffered body, in order, in the shape ``Stream.aiter_events`` yields.

    Each event carries its ``event``, ``data``, ``id`` and ``retry`` as httpx2 parses them. httpx2's ``EventSource``
    parses the grammar (multi-line ``data``, CRLF, comments) but requires a response that declares
    ``text/event-stream``, so the body is framed into a response of its own. ``max_event_size=None`` because the body is
    already buffered and capped by the client, and a large reply is legitimately one event, which httpx2's 1 MiB default
    would refuse.

    ``EventSource`` also yields a block that has no ``data`` field (a ``retry:`` hint, an ``event: ping``), which the
    WHATWG grammar does not dispatch. Such a block is skipped. It cannot be told apart from an event whose ``data`` is
    empty, so that is skipped too. Choosing whether a body is an event stream is the caller's job, usually by reading
    the ``content-type``.
    """
    framed = httpx2.Response(
        200,
        content=resp.content,
        headers={"content-type": "text/event-stream"},
        request=httpx2.Request("POST", _UNSENT_URL),
    )
    return [event for event in httpx2.EventSource(framed, max_event_size=None) if event.data]
