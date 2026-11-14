# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The per-call option bags: :class:`CallOptions`, :class:`RequestOptions` and :class:`StreamOptions`.

Each is a closed ``TypedDict``, so a type checker refuses a keyword it does not name, and an option left out is truly
absent: it inherits the view's, the route's or the policy's, while ``None`` turns it off. A bag is built on a private
base that is not closed, since a closed ``TypedDict`` cannot be extended.

At runtime ``**options`` would swallow any keyword, so every entry point checks the keys it got with
:func:`check_options` before anything is sent, and the options this module names with :func:`check_overrides`.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

# A closed TypedDict (PEP 728) refuses a keyword it does not name, so a stray or misspelled option is a type error.
from typing_extensions import TypedDict

from headgate._core import check_kind
from headgate.retry import RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Mapping

    from headgate._options import Timeouts
    from headgate._request_body import Body, EncodedBody, UploadFiles

__all__ = [
    "REQUEST_KEYS",
    "STREAM_KEYS",
    "WALK_KEYS",
    "CallOptions",
    "RequestOptions",
    "StreamOptions",
    "WalkOptions",
    "check_options",
    "check_overrides",
]


class _EveryCall(TypedDict, total=False):
    """The options every request takes, a verb's, a stream's or a page's."""

    params: Mapping[str, object] | None
    headers: Mapping[str, str]
    timeout: Timeouts
    deadline: float | None
    retry: RetryPolicy | None
    follow_redirects: bool
    idempotency_key: str


class _Buffered(_EveryCall, total=False):
    """The options of a request whose response is read whole: every request's, plus its cache lifetime and size cap."""

    cache_ttl: float | None
    max_response_bytes: int


class _Bodies(TypedDict, total=False):
    """A request's body options."""

    body: EncodedBody
    json: object
    data: Mapping[str, object]
    content: Body
    files: UploadFiles


class CallOptions(_Buffered, total=False, closed=True):
    """The per-call options of every verb and of :meth:`~headgate.AsyncBaseClient.paginate`, beyond the body.

    An option left out takes the view's, the route's or the policy's; ``None`` is off.

    * ``params``: the query, ``Mapping[str, object]`` so that a client can pass a TypedDict (a TypedDict is assignable
      to no narrower mapping). Its values are encoded the way httpx2 encodes a mapping: a list or tuple repeats the key
      once per item, and any value that is not a ``str``, number, bool or ``None`` is sent as its ``str()``.
    * ``headers``: added to the client's; a clash is won by the call.
    * ``timeout``: :class:`~headgate.Timeouts` whose set phases replace the view's or the client's.
    * ``deadline``: seconds this call may take in all, or ``None`` for no deadline.
    * ``retry``: a :class:`~headgate.RetryPolicy` that replaces the view's, the route's or the client's for this call,
      or ``None`` to send once and never re-send. A POST or PATCH is re-sent only when that is safe, whatever policy
      applies.
    * ``follow_redirects``: ``False`` makes a redirect raise as its 3xx; ``True`` follows it.
    * ``cache_ttl``: this response's cache lifetime in seconds, over ``HttpCache.ttl``, or ``None`` for no lifetime
      limit, so the entry is served until its storage evicts it. The cache is TTL-only either way: it never reads the
      response's own cache headers, ``Cache-Control: max-age`` and ``no-store`` included.
    * ``idempotency_key``: the idempotency key this call carries on every attempt, under the policy's
      ``IdempotencyKeys`` header (``Idempotency-Key`` by default).
    * ``max_response_bytes``: the cap on this call's body, counted after decompression, in place of
      ``HttpOptions.max_response_bytes``: higher for one large file, or lower for an endpoint that should stay small. A
      cached client holds the body up to it too.
    """


class RequestOptions(_Buffered, _Bodies, total=False, closed=True):
    """The per-call options a verb takes beyond its URL and ``expect=``: :class:`CallOptions`, plus the body.

    * ``body``: an :class:`~headgate.EncodedBody` a flavor's ``writer`` wrote (``body=NEW_ORDER(order)``), sent
      with its ``Content-Type`` unless ``headers=`` (the call's or its view's, not the client's defaults) sets one. It
      combines with no other body option.
    * ``json`` / ``data``: a JSON body of builtins (a codec's ``to_builtins`` builds one) or a form body.
    * ``content``: a raw body: ``bytes``; an async iterable of ``bytes``, streamed once, so the call is never re-sent
      and a redirect that asks for the body again is not followed; or a callable returning one, called once for each
      send, so the call stays retryable. It combines with no other body option.
    * ``files``: a multipart body, as httpx2 takes it (``{"name": file}``, or ``(filename, file, content_type)``
      tuples), with ``data=`` as its form fields.
    """


class StreamOptions(_EveryCall, _Bodies, total=False, closed=True):
    """The per-call options of :meth:`~headgate.AsyncBaseClient.stream`: :class:`~headgate.RequestOptions` but two.

    A stream is never buffered, so it takes no ``cache_ttl`` and no ``max_response_bytes``; the rest are a verb's.
    """


class WalkOptions[B](_Buffered, total=False, closed=True):
    """What :meth:`~headgate.AsyncBaseClient.paginate`'s implementation takes: the per-page options, and the body."""

    body: B
    json: B


#: The keywords each bag names, read once, for :func:`check_options`.
REQUEST_KEYS: Final = frozenset(RequestOptions.__optional_keys__)
STREAM_KEYS: Final = frozenset(StreamOptions.__optional_keys__)
WALK_KEYS: Final = frozenset(WalkOptions.__optional_keys__)


def check_options(
    where: str, options: Mapping[str, object], allowed: frozenset[str], hints: Mapping[str, str] | None = None
) -> None:
    """Refuse a keyword *where* does not take, as a function with that signature would.

    :raises TypeError: naming the first such keyword in sorted order, and what to write instead when *hints* says.
    """
    if unknown := options.keys() - allowed:
        name = min(unknown)
        msg = f"{where}() got an unexpected keyword argument {name!r}"
        if hints is not None and (hint := hints.get(name)) is not None:
            msg = f"{msg}: {hint}"
        raise TypeError(msg)


def check_overrides(options: Mapping[str, object]) -> None:
    """Check the kind of each override in *options*: ``retry``, ``follow_redirects``, ``cache_ttl`` and the size cap.

    :raises TypeError: for a ``retry`` that is neither a ``RetryPolicy`` nor ``None`` (``True`` and ``False``
        included, with what to write instead), a ``follow_redirects`` that is not a ``bool`` and a ``cache_ttl`` that
        is neither a number nor ``None``, and a ``max_response_bytes`` that is not an ``int`` (a ``bool`` included).
    :raises ValueError: for a ``cache_ttl`` that is not positive and finite, and a ``max_response_bytes`` that is not
        positive.
    """
    if "retry" in options:
        retry = options["retry"]
        if isinstance(retry, bool):
            msg = (
                "retry= takes a RetryPolicy, or None to send once (got bool): write retry=None for retry=False, and "
                "make the call on the client rather than its view for retry=True"
            )
            raise TypeError(msg)
        if retry is not None:
            check_kind("retry", retry, RetryPolicy, "a RetryPolicy, or None to send once")
    if "follow_redirects" in options:
        check_kind("follow_redirects", options["follow_redirects"], bool, "a bool; leave it out to inherit")
    ttl = options.get("cache_ttl")
    if ttl is not None:
        check_kind("cache_ttl", ttl, (int, float), "seconds, or None for no lifetime limit")
    if isinstance(ttl, int | float) and not (ttl > 0 and math.isfinite(ttl)):
        msg = f"cache_ttl must be a positive finite number of seconds, or None (got {ttl})"
        raise ValueError(msg)
    if "max_response_bytes" in options:
        cap = options["max_response_bytes"]
        if isinstance(cap, bool) or not isinstance(cap, int):
            # No kind at all, so check_kind refuses a bool too, in its own words.
            check_kind("max_response_bytes", cap, (), "a number of bytes, an int")
        elif cap <= 0:
            msg = f"max_response_bytes must be > 0 (got {cap})"
            raise ValueError(msg)
