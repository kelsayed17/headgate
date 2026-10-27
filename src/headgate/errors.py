# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The failure taxonomy: raised by clients, caught by their callers.

Every layer above a client imports this module, so it imports nothing at runtime beyond the standard library.

The shape of the tree is part of the contract, as well as the names: a boundary handler can map
``ClientTimeoutError`` to 504, ``ClientConfigurationError`` to 503 and every other ``ClientError`` to 502, and
re-parenting a class changes that mapping.

:class:`DecodeError` is deliberately not a ``ClientError``. A codec raises it for a body it cannot decode, and the base
client turns it into ``ResponseParseError`` (a 2xx body) or the status error (a declared non-2xx), as it does the
``ValueError``, ``LookupError`` or ``TypeError`` a hand-written reader raises for a body of the wrong shape.

Every ``ClientError`` pickles, so it can cross a process or a durable-workflow boundary. The default exception reduce,
``(cls, self.args)``, re-runs ``__init__``: that fails on a required keyword-only argument (``status_code``) and would
prefix the message a second time. :meth:`ClientError.__reduce__` rebuilds the instance without ``__init__`` instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, TypedDict, Unpack, override

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "AuthError",
    "CircuitOpenError",
    "ClientConfigurationError",
    "ClientError",
    "ClientTimeoutError",
    "DecodeError",
    "ErrorDetails",
    "ForbiddenDestinationError",
    "PageLimitError",
    "Problem",
    "RateLimitError",
    "ResponseParseError",
    "ResponseTooLargeError",
    "TransportError",
    "UpstreamStatusError",
]


class DecodeError(Exception):
    """A response body could not be decoded into the declared type; raised by every codec flavor."""


def _is_absent(text: str | None) -> bool:
    """Whether an optional message part is missing: ``None``, or empty, which would render as a bare delimiter."""
    return text is None or text == ""


type _ClientErrorState = tuple[type[ClientError], tuple[object, ...], dict[str, object]]
type _ClientErrorRebuild = Callable[[type[ClientError], tuple[object, ...], dict[str, object]], ClientError]


def _rebuild_client_error(cls: type[ClientError], args: tuple[object, ...], state: dict[str, object]) -> ClientError:
    """Reconstruct a pickled ``ClientError`` without re-running ``__init__``, restoring its ``args`` and attributes."""
    obj = cls.__new__(cls)
    obj.args = args
    obj.__dict__.update(state)
    return obj


class ErrorDetails(TypedDict, total=False):
    """What a ``ClientError`` records about the call that raised it, beyond its message; every key is optional.

    ``source`` names the client that raised it. ``url`` is sanitized and bounded. ``attempts`` counts the call's sends;
    ``retryable`` says whether the call's retry policy would re-send after the outcome that raised the error (a status
    in its ``statuses`` or a network failure, and only when sending again is safe for the method); ``retry_after`` is
    the delay the upstream asked for, in seconds.
    """

    source: str | None
    method: str | None
    url: str | None
    request_id: str | None
    attempts: int | None
    retryable: bool
    retry_after: float | None


class ClientError(Exception):
    """Base for every transport-layer failure.

    ``source`` names the originating client and prefixes the message as ``[source]``. Subclasses distinguish failure
    modes so callers can branch on them. The read-only attributes :attr:`method`, :attr:`url`, :attr:`request_id`,
    :attr:`attempts`, :attr:`retryable` and :attr:`retry_after` describe the call (see :class:`ErrorDetails`); each is
    ``None`` (``retryable``: ``False``) when the raiser did not know it.
    """

    def __init__(self, message: str, **details: Unpack[ErrorDetails]) -> None:
        """Record *details*, and build the message, prefixed with ``[source]`` when a source is named."""
        self.source = source = details.get("source")
        self._method = details.get("method")
        self._url = details.get("url")
        self._request_id = details.get("request_id")
        self._attempts = details.get("attempts")
        self._retryable = details.get("retryable", False)
        self._retry_after = details.get("retry_after")
        super().__init__(message if _is_absent(source) else f"[{source}] {message}")

    @property
    def method(self) -> str | None:
        """The request method of the call that failed."""
        return self._method

    @property
    def url(self) -> str | None:
        """The URL of the call that failed, credentials redacted and bounded."""
        return self._url

    @property
    def request_id(self) -> str | None:
        """The upstream's request id for the final response, from the first ``request_id_headers`` header it sent."""
        return self._request_id

    @property
    def attempts(self) -> int | None:
        """How many sends the call made, retries and followed redirects included."""
        return self._attempts

    @property
    def retryable(self) -> bool:
        """Whether sending the call again could succeed."""
        return self._retryable

    @property
    def retry_after(self) -> float | None:
        """The seconds the upstream (or an open breaker) asked the caller to wait before sending again."""
        return self._retry_after

    @override
    def __reduce__(self) -> tuple[_ClientErrorRebuild, _ClientErrorState]:
        """Reduce to :func:`_rebuild_client_error`, which every subclass shares through ``type(self)``."""
        return (_rebuild_client_error, (type(self), self.args, dict[str, object](self.__dict__)))


class TransportError(ClientError):
    """Network or connection failure that survived retries, or a refusal by an open circuit breaker."""


class ClientTimeoutError(TransportError):
    """Connect or read timeout that survived retries, or the call's deadline passing; a boundary would map it to 504."""


class CircuitOpenError(TransportError):
    """The host's circuit breaker refused the call without sending it; ``retry_after`` is the cooldown left."""


def _freeze(value: object) -> object:
    """Return a JSON value with every object made a read-only mapping and every array a tuple, at any depth."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    """Return a frozen JSON value (see :func:`_freeze`) as plain dicts and lists, which pickle."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


#: What a :class:`Problem` pickles as: its fields in order, ``extensions`` thawed.
type _ProblemState = tuple[str, str | None, int | None, str | None, str | None, object]


@dataclass(frozen=True, slots=True, kw_only=True)
class Problem:
    """An RFC 9457 problem detail an upstream sent as ``application/problem+json``.

    The standard members are typed; a member of the wrong JSON type is ignored, as the RFC requires. Every other member
    is in ``extensions``, read-only at any depth. ``type`` defaults to ``"about:blank"``.
    """

    type: str = "about:blank"
    title: str | None = None
    status: int | None = None
    detail: str | None = None
    instance: str | None = None
    extensions: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze ``extensions``, at every depth, into read-only mappings and tuples."""
        object.__setattr__(self, "extensions", _freeze(self.extensions))

    @override
    def __getstate__(self) -> _ProblemState:
        """Return the fields with ``extensions`` thawed, since a read-only mapping does not pickle."""
        return (self.type, self.title, self.status, self.detail, self.instance, _thaw(self.extensions))

    def __setstate__(self, state: _ProblemState) -> None:
        """Restore the fields from :meth:`__getstate__`, freezing ``extensions`` again."""
        for name, value in zip(("type", "title", "status", "detail", "instance"), state, strict=False):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "extensions", _freeze(state[-1]))


class UpstreamStatusError(ClientError):
    """The upstream answered with a failure: a non-2xx that survived retries, or a refusal written in a 2xx body.

    ``status_code`` is the status the wire sent, or the endpoint's documented success status for a refusal written
    inside a success body. It is never invented.

    ``code`` is the provider's own code where something named one (a reader's ``RefusedError``). The status ladder sets
    none, so an undeclared 4xx or 5xx carries ``code=None`` and its body only as the snippet in the message. An empty
    code is still a code, and the message names it.

    ``body`` is a bounded, single-line snippet of the response body, folded into the message: the upstream's own reason
    for a 4xx is the diagnosis, and dropping it forces a manual reproduction just to read it.

    ``problem`` is the RFC 9457 problem detail the body carried, if it was one; the message then names its title and
    detail instead of the raw body.

    Every subclass shares this constructor. A subclass names what kind of refusal it was (a credential, a rate limit or
    quota), never what status it arrived with.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None = None,
        body: str | None = None,
        problem: Problem | None = None,
        **details: Unpack[ErrorDetails],
    ) -> None:
        """Record the status, code and problem, and fold the status, code and body snippet into the message."""
        self.status_code = status_code
        self.code = code
        self._problem = problem
        tag = f"status={status_code}" if code is None else f"status={status_code}, code={code}"
        detail = "" if _is_absent(body) else f" — {body}"
        super().__init__(f"{message} ({tag}){detail}", **details)

    @property
    def problem(self) -> Problem | None:
        """The RFC 9457 problem detail the response body carried, or ``None``."""
        return self._problem


class RateLimitError(UpstreamStatusError):
    """The provider's rate limit or quota refused us.

    A 429 that survived retries, or a quota refusal the provider wrote inside a 200 body. The status is whichever the
    wire sent. An in-band one bypasses the retry backoff and the breaker, because a 200 looks healthy to both, so
    retrying it is the caller's call.
    """


class AuthError(UpstreamStatusError):
    """Our own credential was refused.

    A 401 or 403 from the status ladder, or a host's refusal naming the credential in another shape (a redirect to a
    sign-in page, which a client's ``refusal_check=`` check names). The status is never invented.
    """


class ClientConfigurationError(ClientError):
    """Our own configuration is wrong; no retry can fix it.

    Distinct from :class:`AuthError`, which is a 401/403 the upstream returned. This is raised without any bad status at
    all: the exchange succeeded and told us something about our own setup that the client cannot work with (a rotating
    credential where a static one is required, say). An operator has to change something.
    """


class ResponseParseError(ClientError):
    """The body could not be decoded or validated into the expected type.

    Raised for a 2xx body its reader could not read, naming the status, the request line and the reader's diagnosis
    (bounded); and for a token response a ``CachedTokenAuth`` could not read. ``status_code`` and ``content_type`` are
    the response's, when there was one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        content_type: str | None = None,
        **details: Unpack[ErrorDetails],
    ) -> None:
        """Record the response's status and content type beside the message."""
        self._status_code = status_code
        self._content_type = content_type
        super().__init__(message, **details)

    @property
    def status_code(self) -> int | None:
        """The status of the response whose body could not be read."""
        return self._status_code

    @property
    def content_type(self) -> str | None:
        """The ``Content-Type`` the response declared."""
        return self._content_type


class ResponseTooLargeError(ClientError):
    """The response body exceeded the client's size ceiling and the transfer was aborted.

    Raised before the body is fully buffered, so an upstream cannot exhaust memory. The decompressed byte count is what
    is measured, since ``Content-Length`` reports the compressed size and a gzip bomb clears any header check. A plain
    ``ClientError``, so a boundary treats it as the upstream misbehaving.
    """


class ForbiddenDestinationError(ClientError):
    """A request was refused before it was sent, because of where it was going.

    Raised by the SSRF guard (``SecurityOptions(allow_private_hosts=False)``) for a host that is, or resolves to, an
    address that is not globally routable: loopback, private, link-local, multicast, reserved or unspecified,
    IPv4-mapped and NAT64 forms included. The destination usually comes from data (a redirect, a cursor URL, a URL a
    user supplied) rather than from configuration, so it is its own subtype rather than a
    :class:`ClientConfigurationError`: a boundary can answer it as the caller's bad input. Nothing was sent, so it is
    never retried and never charged to a circuit breaker.
    """


class PageLimitError(ClientError):
    """A paginated walk reached its page budget (``paginate(..., max_pages=...)``) with more pages still to come.

    The pages fetched so far were yielded; what follows was not fetched, so the result is incomplete. ``pages`` is the
    number of pages fetched. A walk whose last page falls exactly on the budget ends normally, and
    ``on_limit="stop"`` ends a walk at the budget without raising.
    """

    def __init__(self, message: str, *, pages: int, **details: Unpack[ErrorDetails]) -> None:
        """Record *pages*, the pages fetched, with the *details* of the page that would have come next."""
        super().__init__(message, **details)
        self.pages = pages
