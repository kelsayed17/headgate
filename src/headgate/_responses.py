# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a client call declares about the responses it can get back.

A client method is one provider endpoint, and it returns that endpoint's documented outcome as a typed value or raises
a ``ClientError``. So each call declares, where it is made, what it can get back:

* a :data:`Reader` for any 2xx: the success reader;
* optionally an :class:`Expect` that adds the exact non-2xx statuses the provider DOCUMENTS as answers
  (:meth:`Expect.on`).

Anything a call did not declare raises through the base client's status ladder. There is no raw mode, so no call can
receive an undeclared status. A refusal the provider writes inside a body is raised by the reader as :class:`Refused`,
and the base client turns it into a ``ClientError`` carrying the status the wire actually sent.

:data:`Reader` · :class:`Expect` · :class:`Refused` · :data:`RefusalCheck` · :func:`json_reader` · :func:`as_bytes` ·
:func:`nothing` · :func:`event_stream` — each documented on itself.

This module is codec-free: it imports neither msgspec nor pydantic. The JSON ``decoder`` a client uses to build its
success reader comes from the flavor module it chose, :mod:`headgate.msgspec` or :mod:`headgate.pydantic`, and both
are built on :func:`json_reader` here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

import httpx2

from headgate.errors import UpstreamStatusError

__all__ = ["Expect", "Reader", "RefusalCheck", "Refused", "as_bytes", "event_stream", "json_reader", "nothing"]


type Reader[T] = Callable[[httpx2.Response], T]
"""Turns a response into a value.

It receives the WHOLE response (status, headers, body), because a reader may need what a header declares (a
``content-type`` choosing JSON or an event stream). It may raise :class:`~headgate.errors.DecodeError` (the body is
not what was declared) or :class:`Refused` (the provider said no inside the body). It never builds a ``ClientError``
itself: the base client does, so the error carries the real status and the client's name.
"""

#: Statuses that always mean "we could not ask", never an answer about the request: our credentials (401, 407) and
#: the rate limit the retry transport and the breaker already own (429). Every 5xx is refused by range, beside these.
_COULD_NOT_ASK: Final = frozenset(
    {httpx2.codes.UNAUTHORIZED, httpx2.codes.PROXY_AUTHENTICATION_REQUIRED, httpx2.codes.TOO_MANY_REQUESTS}
)


#: The status range RFC 9110 defines.
_MIN_STATUS, _MAX_STATUS = 100, 599


def _refuse_unclaimable(status: int) -> None:
    """Raise ``ValueError`` for a status no call may declare as an answer. The ONE check both :class:`Expect` paths run.

    The constructor runs it over every key of ``answers`` and :meth:`Expect.on` over the status it adds, so the two
    paths refuse the same statuses with the same message and cannot drift apart.
    """
    if not _MIN_STATUS <= status <= _MAX_STATUS:
        msg = f"{status} is not an HTTP status"
        raise ValueError(msg)
    if status in _COULD_NOT_ASK or status >= httpx2.codes.INTERNAL_SERVER_ERROR:
        msg = f"{status} means we could not ask; it is never a documented answer"
        raise ValueError(msg)


class Refused(Exception):  # noqa: N818  # a signal a reader raises, like StopIteration; not an error class
    """A provider's refusal, as its reader (or a host check) saw it. The base client builds the ``ClientError``.

    Raised by a reader for a refusal written inside a body: an error envelope, a ``REQUEST_NOT_PROCESSED`` status, a
    vendor error code. It is also returned by a ``decoder`` ``check``, which ``decoder`` raises.
    :meth:`~headgate._client.AsyncBaseClient._request` is its ONLY catcher. There it becomes ``kind(message,
    status_code=<the status the wire sent>, code=code, source=<client>, body=<bounded snippet>)``, so a refusal in a 200
    body carries 200.

    ``code`` is the provider's own code. ``kind`` is the ``ClientError`` class that code documents: ``AuthError`` for
    a credential, ``RateLimitError`` for a quota or rate limit, and ``UpstreamStatusError`` otherwise.

    An exception rather than a return value: returning ``T | Refused`` would widen every reader's type and blur a
    refusal with the value markers a call's readers decode to, which ARE data.
    """

    def __init__(
        self, message: str, *, code: str | None = None, kind: type[UpstreamStatusError] = UpstreamStatusError
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.kind = kind


type RefusalCheck = Callable[[httpx2.Response], Refused | None]
"""A per-HOST check, run on the final response before anything the call declared.

It names a refusal the status line alone cannot show, such as a key-error redirect, and returns ``None`` for everything
else. It reads the status line and headers, never the body: a refusal written in a body is its reader's to raise. A
client carrying one must not follow redirects (the base client enforces it), because the check sees only the final
response.
"""


@dataclass(frozen=True, slots=True)
class Expect[T]:
    """One endpoint's documented responses: a success reader for any 2xx, plus exact-status answers. Never callable.

    ``answers`` holds the statuses the provider DOCUMENTS as answers, each declared with :meth:`on` inside the client
    method whose endpoint returns it. The base client reads an exact declared status first, applies its status ladder
    to any other non-2xx, and hands every other 2xx to :attr:`ok`. A declared non-2xx whose body its reader cannot
    decode falls through to the ladder with its real status, so a claim covers only the body shape it names: a
    declared business 403 never swallows a real permission 403.

    Separate from :data:`Reader` on purpose. A verb takes ``Reader[T] | Expect[T]``, and that union is unambiguous
    only because an ``Expect`` is not callable.

    The constructor refuses exactly what :meth:`on` refuses (a status outside 100-599, 401, 407, 429 or any 5xx),
    because an ``Expect`` built directly with ``answers`` is the same claim. It then freezes ``answers`` into a
    read-only copy, so neither the caller's dict nor the default can be changed afterwards.
    """

    ok: Reader[T]
    answers: Mapping[int, Reader[T]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for status in self.answers:
            _refuse_unclaimable(status)
        # A frozen, slotted dataclass admits no plain assignment, even here; this is the documented escape hatch.
        object.__setattr__(self, "answers", MappingProxyType(dict(self.answers)))

    def on[E](self, status: int, read: Reader[E]) -> Expect[T | E]:
        """Declare *status* as a documented answer read by *read*. The result type widens to ``T | E``.

        Refused at construction with ``ValueError``, so a bad claim fails when its module is imported:

        * a status outside 100-599;
        * 401, 407, 429 or any 5xx. By fiat these mean we could not ask: the credentials are ours, and the transport
          owns 429 and 5xx through retry and the breaker;
        * a status already declared.

        No ranges and no ``default``: OpenAPI allows ``4XX``, but a range is a claim about statuses nobody documented.
        The result is a NEW ``Expect`` over a fresh mapping; this one is left as it was.
        """
        _refuse_unclaimable(status)
        if status in self.answers:
            msg = f"{status} is already declared on this Expect"
            raise ValueError(msg)
        widened: dict[int, Reader[T | E]] = {**self.answers, status: read}
        return Expect(self.ok, widened)


def json_reader[T](
    decode: Callable[[bytes], T],
    *,
    allow_empty_body: bool = False,
    check: Callable[[T], Refused | None] | None = None,
) -> Reader[T]:
    """Build a reader around a codec's bytes-to-value function. The one place the three steps of a JSON read live.

    Each codec flavor's ``decoder`` binds its own ``decode`` (with ``type_`` and ``strict`` already applied) and calls
    this, so the empty-body rule and the ``check`` hook behave identically across flavors.

    ``allow_empty_body`` reads a truly empty body (zero bytes or whitespace) as the empty ARRAY ``[]``. It suits only a
    list-shaped type, for a provider whose no-data answer is a zero-byte 200 or a 204. For every other provider an empty
    200 is corruption and fails loud.

    ``check`` runs on the DECODED value and returns a :class:`Refused` for a refusal written inside a 2xx body, or
    ``None``. The reader raises what it returns. A check is typed over a small structural ``Protocol`` naming only the
    envelope fields it reads. It raises only for codes EVERY consumer of the endpoint would read as failure: auth,
    quota, rate limit, server, a malformed request, and any undocumented code. A code about the subject (an unknown
    instrument, a rejected order) is left in the value for the caller.
    """

    def _read(resp: httpx2.Response, /) -> T:
        content = resp.content
        if allow_empty_body and not content.strip():
            content = b"[]"
        value = decode(content)
        if check is not None and (refused := check(value)) is not None:
            raise refused
        return value

    return _read


def as_bytes(resp: httpx2.Response, /) -> bytes:
    """Return the body undecoded: a wire that is not a *format* from the client's point of view.

    A CSV/TXT/ZIP/PDF/XML dump a parser reads later, or an opaque blob. It never raises. Named ``as_bytes`` rather than
    ``body`` because ``body`` is the commonest parameter name in a client, and any of them would shadow the import.
    """
    return resp.content


def nothing(resp: httpx2.Response, /) -> None:
    """No value: the call's answer is that it completed. For a void write (a cancel, a delete, a reset) and HEAD.

    A method declaring it returns ``None`` only by finishing, and every failure raised on the way. Await the call as a
    statement: type checkers reject binding a ``None``-typed call.

    Throwing a body away is a claim about the wire, so declare ``nothing`` only for an endpoint documented to send no
    body you need.
    """
    del resp


#: A reserved (RFC 2606) URL for the request an ``httpx2.Response`` must carry. Nothing is ever sent to it.
_UNSENT_URL: Final = "https://sse.invalid/"


def event_stream(resp: httpx2.Response, /) -> list[bytes]:
    """Return the ``data`` of each server-sent event in the body, in order.

    httpx2's ``EventSource`` parses the grammar (multi-line ``data``, CRLF, comments) but requires a response that
    declares ``text/event-stream``, so the body is framed into an ``httpx2.Response`` of its own first.
    ``max_event_size=None``: the body is already buffered and capped by the client, and a large reply is legitimately
    one event, which httpx2's 1 MiB default would refuse. Choosing WHETHER a body is an event stream is the caller's
    job, usually by reading the ``content-type``.
    """
    framed = httpx2.Response(
        200,
        content=resp.content,
        headers={"content-type": "text/event-stream"},
        request=httpx2.Request("POST", _UNSENT_URL),
    )
    return [event.data.encode() for event in httpx2.EventSource(framed, max_event_size=None)]
