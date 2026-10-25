# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The ``ClientError`` a failed exchange becomes: pure builders.

Each builder returns its error rather than raising it, so the caller raises it with the right cause (``from exc`` for
a transport failure, ``from refused`` for a reader's :class:`~headgate._responses.RefusedError`). Each takes
``source=``, the client class's name, which every ``ClientError`` carries.

Every message here can reach an application boundary's error detail, so no builder puts a raw URL, an unbounded text
or a credential in one. A ``log_url`` argument is already sanitized and shortened (:func:`loggable_url`); every other
text that comes from outside (an exception's message, a body) is bounded with :func:`shorten`. The status errors name
the request line as ``METHOD log_url``, so a 404, a surviving 503 and an undecodable 200 read alike.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx2

from headgate._url import redact_secrets, sanitize_url
from headgate.errors import (
    AuthError,
    ClientError,
    ClientTimeoutError,
    DecodeError,
    RateLimitError,
    ResponseParseError,
    TransportError,
    UpstreamStatusError,
)

if TYPE_CHECKING:
    from headgate._responses import RefusedError

__all__ = [
    "MESSAGE_TEXT_LIMIT",
    "SHAPE_ERRORS",
    "body_snippet",
    "is_transient_status",
    "loggable_url",
    "mint_error",
    "parse_error",
    "refusal_error",
    "shorten",
    "status_error",
    "transient_status_error",
    "transport_error",
]

#: Cap on the error-body snippet folded into a ClientError message: enough for a provider's structured reason, not
#: enough to turn an HTML error page into a payload.
_BODY_SNIPPET_LIMIT = 300

#: How much of an error body is decoded to find the snippet. Bounds the work on a body up to the response cap, while
#: leaving room for the whitespace an HTML page spends before its text.
_SNIPPET_SCAN_BYTES = 16 * 1024

#: Cap on a URL named in a message. A URL may legitimately run to httpx2's 65,536-character limit, and a message that
#: repeats it whole is a log line nobody can read.
_URL_LIMIT = 300

#: Cap on any other outside text folded into a message: an exception's own message, a decoder's diagnosis.
MESSAGE_TEXT_LIMIT = 1000

#: The statuses that name our own credential as the failure.
_AUTH_STATUSES = frozenset({httpx2.codes.UNAUTHORIZED, httpx2.codes.FORBIDDEN})

#: What a reader or a token mint raises for a body that is not the shape it expected: a codec's ``DecodeError``, and
#: what hand-written code over ``response.json()`` raises (``JSONDecodeError`` is a ``ValueError``; a missing key, an
#: index past the end, or indexing the wrong container).
SHAPE_ERRORS: tuple[type[Exception], ...] = (DecodeError, ValueError, LookupError, TypeError)

#: A header name as RFC 9110 defines it (a ``token``), the only kind of name a message repeats.
_HEADER_NAME = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

#: A header value h11 accepts: bytes other than NUL and whitespace, with spaces or tabs only between them.
_HEADER_VALUE = re.compile(rb"(?:[^\x00\s]+(?:[ \t]+[^\x00\s]+)*)?")


def shorten(text: str, limit: int = MESSAGE_TEXT_LIMIT) -> str:
    """Return *text* cut to at most *limit* characters, marked with a trailing ``…`` when cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def loggable_url(url: str) -> str:
    """Return *url* as every message names it: credentials redacted (:func:`sanitize_url`), then shortened."""
    return shorten(sanitize_url(url), _URL_LIMIT)


def body_snippet(resp: httpx2.Response) -> str | None:
    """Return a bounded, single-line view of an error response body, or ``None`` for a blank one.

    The upstream's own reason for a 4xx is the diagnosis (a 400 that names the missing field), so it is folded into the
    ``ClientError`` message, bounded and whitespace-collapsed because that message may reach an API response.
    """
    head = resp.content[:_SNIPPET_SCAN_BYTES].decode(resp.encoding or "utf-8", errors="replace")
    text = " ".join(head.split())
    if not text:
        return None
    if len(text) > _BODY_SNIPPET_LIMIT:
        return text[:_BODY_SNIPPET_LIMIT] + "…"
    return text


def is_transient_status(status: int) -> bool:
    """Return whether *status* is one the retry transport re-sends and the breaker counts: 429 or any 5xx."""
    return status == httpx2.codes.TOO_MANY_REQUESTS or status >= httpx2.codes.INTERNAL_SERVER_ERROR


def status_error(resp: httpx2.Response, method: str, log_url: str, *, source: str) -> UpstreamStatusError:
    """Build the ``ClientError`` for a non-success status the call did not declare: the status ladder.

    401/403 become :class:`~headgate.errors.AuthError`, because a bad credential is this client's configuration
    failing against the upstream, not the caller's request. Any other status becomes
    :class:`~headgate.errors.UpstreamStatusError`. Both carry the real status and a bounded body snippet.
    """
    kind = AuthError if resp.status_code in _AUTH_STATUSES else UpstreamStatusError
    return kind(
        f"http error for {method} {log_url}", status_code=resp.status_code, source=source, body=body_snippet(resp)
    )


def refusal_error(refused: RefusedError, resp: httpx2.Response, log_url: str, *, source: str) -> UpstreamStatusError:
    """Build the ``ClientError`` for a provider's :class:`RefusedError`, from a reader or the host's ``refusal_check``.

    Both paths build the error here, so both carry the provider's code, the client's name and the body snippet.
    ``refused.kind`` names the class, and the status is the one the wire sent: a refusal written inside a 200 body
    carries 200, and a host's key-error redirect carries its 302.
    """
    return refused.kind(
        f"{refused.message} ({log_url})",
        status_code=resp.status_code,
        code=refused.code,
        source=source,
        body=body_snippet(resp),
    )


def parse_error(exc: Exception, resp: httpx2.Response, method: str, log_url: str, *, source: str) -> ResponseParseError:
    """Build the ``ClientError`` for a 2xx body its reader could not read (one of :data:`SHAPE_ERRORS`).

    The message names the status and the request line, and the reader's diagnosis bounded by :func:`shorten`.
    """
    detail = shorten(str(exc)) or type(exc).__name__
    return ResponseParseError(f"unreadable {resp.status_code} body for {method} {log_url}: {detail}", source=source)


def transport_error(exc: httpx2.HTTPError | OSError, method: str, log_url: str, *, source: str) -> TransportError:
    """Build the ``ClientError`` for a network, protocol or timeout failure: nothing usable came back.

    httpx2 names the URL in most of its own messages but a bare ``OSError`` names nothing, so the request line is
    stated explicitly. Many httpx2 exceptions stringify to ``""`` (a dropped connection, a read timeout), and the type
    name is then the only record of what failed. A timeout becomes :class:`~headgate.errors.ClientTimeoutError`.

    An ``httpx2.LocalProtocolError`` gets a fixed message instead (see :func:`_unsendable_detail`): it is the HTTP
    library refusing to encode the request, and its own message quotes the offending header value, which is usually
    the credential. Its caller raises the result ``from None`` for the same reason.
    """
    if isinstance(exc, httpx2.LocalProtocolError):
        detail = _unsendable_detail(exc)
    else:
        detail = shorten(redact_secrets(str(exc))) or type(exc).__name__
    message = f"{method} {log_url}: {detail}"
    if isinstance(exc, httpx2.TimeoutException):
        return ClientTimeoutError(message, source=source)
    return TransportError(message, source=source)


def _unsendable_detail(exc: httpx2.LocalProtocolError) -> str:
    """Describe a request the HTTP library refused to encode without repeating any of its values.

    The names of the headers whose values are not legal field values are given when the request is known, since that
    is what an operator needs to find the bad setting; a name that is not itself a legal token is not repeated.
    """
    detail = "the request could not be encoded (LocalProtocolError)"
    try:
        request = exc.request
    except RuntimeError:  # httpx2 raises when the error was built without a request
        return detail
    bad = [name for name, value in request.headers.raw if not _HEADER_VALUE.fullmatch(value)]
    if not bad:
        return detail
    shown = ", ".join(name.decode() if _HEADER_NAME.fullmatch(name) else "<invalid name>" for name in bad)
    return f"{detail}: illegal value in header {shown}"


def transient_status_error(resp: httpx2.Response, method: str, log_url: str, *, source: str) -> UpstreamStatusError:
    """Build the ``ClientError`` for a 429 or 5xx that outlasted the retry transport's attempts.

    A 429 becomes :class:`~headgate.errors.RateLimitError`, anything else
    :class:`~headgate.errors.UpstreamStatusError`. *resp* carries a bounded head of the body, which becomes the
    snippet, as it does for :func:`status_error`.
    """
    code = resp.status_code
    kind = RateLimitError if code == httpx2.codes.TOO_MANY_REQUESTS else UpstreamStatusError
    return kind(f"transient {code} for {method} {log_url}", status_code=code, source=source, body=body_snippet(resp))


def mint_error(exc: Exception, *, source: str) -> Exception:
    """Map a failure raised while a :class:`~headgate.CachedTokenAuth` minted its token onto the ``ClientError`` tree.

    The mint usually runs on a client of its own, so its failure names the token endpoint, never the API URL of the
    call that needed the token, and it is raised from the auth flow rather than the transport, so the API host's
    circuit breaker records nothing for it:

    * a ``ClientError`` is returned as it is;
    * ``httpx2.HTTPStatusError`` (a mint's ``raise_for_status()``) becomes the status ladder's error for the token
      endpoint: ``AuthError`` for 401/403, ``UpstreamStatusError`` otherwise;
    * any other ``httpx2.HTTPError`` or an ``OSError`` becomes ``TransportError`` / ``ClientTimeoutError``;
    * one of :data:`SHAPE_ERRORS` (an unreadable token response) becomes ``ResponseParseError``.

    Anything else is returned as it is, for the caller to re-raise: it is a bug in the mint, not a failure to report.
    """
    if isinstance(exc, ClientError):
        return exc
    if isinstance(exc, httpx2.HTTPStatusError):
        resp = exc.response
        method, log_url = _request_line(exc)
        kind = AuthError if resp.status_code in _AUTH_STATUSES else UpstreamStatusError
        try:
            body = body_snippet(resp)
        except httpx2.ResponseNotRead:  # a mint that checked the status of a streamed response before reading it
            body = None
        return kind(f"token mint failed for {method} {log_url}", status_code=resp.status_code, source=source, body=body)
    if isinstance(exc, httpx2.HTTPError | OSError):
        method, log_url = _request_line(exc)
        return transport_error(exc, method, f"{log_url} (token mint)", source=source)
    if isinstance(exc, SHAPE_ERRORS):
        detail = shorten(redact_secrets(str(exc))) or type(exc).__name__
        return ResponseParseError(f"unreadable token response: {detail}", source=source)
    return exc


def _request_line(exc: httpx2.HTTPError | OSError) -> tuple[str, str]:
    """Return the method and loggable URL of the request *exc* names, or placeholders when it names none."""
    try:
        request = exc.request if isinstance(exc, httpx2.HTTPError) else None
    except RuntimeError:  # httpx2 raises when the error was built without a request
        request = None
    if request is None:
        return "token", "endpoint"
    return request.method, loggable_url(str(request.url))
