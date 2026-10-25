# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The failure taxonomy: raised by clients, caught by their callers.

Every layer above a client imports this module, so it imports nothing at runtime beyond ``typing``.

The shape of the tree is part of the contract, as well as the names: a boundary handler can map
``ClientTimeoutError`` to 504, ``ClientConfigurationError`` to 503 and every other ``ClientError`` to 502, and
re-parenting a class changes that mapping.

:class:`DecodeError` is deliberately not a ``ClientError``. A codec raises it for a body it cannot decode, and the base
client turns it into ``ResponseParseError`` (a 2xx body) or the status error (a declared non-2xx).

Every ``ClientError`` pickles, so it can cross a process or a durable-workflow boundary. The default exception reduce,
``(cls, self.args)``, re-runs ``__init__``: that fails on a required keyword-only argument (``status_code``) and would
prefix the message a second time. :meth:`ClientError.__reduce__` rebuilds the instance without ``__init__`` instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "AuthError",
    "ClientConfigurationError",
    "ClientError",
    "ClientTimeoutError",
    "DecodeError",
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


class ClientError(Exception):
    """Base for every transport-layer failure.

    ``source`` names the originating client and prefixes the message as ``[source]``. Subclasses distinguish failure
    modes so callers can branch on them.
    """

    def __init__(self, message: str, *, source: str | None = None) -> None:
        """Record *source* and build the message, prefixed with ``[source]`` when a source is named."""
        self.source = source
        super().__init__(message if _is_absent(source) else f"[{source}] {message}")

    @override
    def __reduce__(self) -> tuple[_ClientErrorRebuild, _ClientErrorState]:
        """Reduce to :func:`_rebuild_client_error`, which every subclass shares through ``type(self)``."""
        return (_rebuild_client_error, (type(self), self.args, dict[str, object](self.__dict__)))


class TransportError(ClientError):
    """Network or connection failure that survived retries, or a refusal by an open circuit breaker."""


class ClientTimeoutError(TransportError):
    """Connect or read timeout that survived retries; a boundary would map it to 504."""


class UpstreamStatusError(ClientError):
    """The upstream answered with a failure: a non-2xx that survived retries, or a refusal written in a 2xx body.

    ``status_code`` is the status the wire sent, or the endpoint's documented success status for a refusal written
    inside a success body. It is never invented.

    ``code`` is the provider's own code where something named one (a reader's ``RefusedError``). The status ladder sets
    none, so an undeclared 4xx or 5xx carries ``code=None`` and its body only as the snippet in the message. An empty
    code is still a code, and the message names it.

    ``body`` is a bounded, single-line snippet of the response body, folded into the message: the upstream's own reason
    for a 4xx is the diagnosis, and dropping it forces a manual reproduction just to read it.

    Every subclass shares this constructor. A subclass names what kind of refusal it was (a credential, a rate limit or
    quota), never what status it arrived with.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None = None,
        source: str | None = None,
        body: str | None = None,
    ) -> None:
        """Record the status and code, and fold them and the body snippet into the message."""
        self.status_code = status_code
        self.code = code
        tag = f"status={status_code}" if code is None else f"status={status_code}, code={code}"
        detail = "" if _is_absent(body) else f" — {body}"
        super().__init__(f"{message} ({tag}){detail}", source=source)


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
    """The body could not be decoded or validated into the expected type."""


class ResponseTooLargeError(ClientError):
    """The response body exceeded the client's size ceiling and the transfer was aborted.

    Raised before the body is fully buffered, so an upstream cannot exhaust memory. The decompressed byte count is what
    is measured, since ``Content-Length`` reports the compressed size and a gzip bomb clears any header check. A plain
    ``ClientError``, so a boundary treats it as the upstream misbehaving.
    """
