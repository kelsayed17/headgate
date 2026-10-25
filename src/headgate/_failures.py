# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The ``ClientError`` a failed exchange becomes: pure builders.

Each builder returns its error rather than raising it, so the caller raises it with the right cause (``from exc`` for
a transport failure, ``from refused`` for a reader's :class:`~headgate._responses.RefusedError`). Each takes the
:class:`CallSite` it failed at: the client class's name, the request line and the attempt count, which every
``ClientError`` carries with the response's request id, whether a retry could succeed, and any ``Retry-After``.

An ``application/problem+json`` body (RFC 9457) is parsed into a :class:`~headgate.errors.Problem` on the status
errors, bounded to :data:`_PROBLEM_SCAN_BYTES`, and its title and detail replace the raw body snippet in the message.

Every message here can reach an application boundary's error detail, so no builder puts a raw URL, an unbounded text
or a credential in one. A ``log_url`` argument is already sanitized and shortened (:func:`loggable_url`); every other
text that comes from outside (an exception's message, a body) is bounded with :func:`shorten`. The status errors name
the request line as ``METHOD log_url``, so a 404, a surviving 503 and an undecodable 200 read alike.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import httpx2

from headgate._core import retry_after_of
from headgate._url import redact_secrets, sanitize_url
from headgate.errors import (
    AuthError,
    ClientError,
    ClientTimeoutError,
    DecodeError,
    ErrorDetails,
    Problem,
    RateLimitError,
    ResponseParseError,
    TransportError,
    UpstreamStatusError,
)
from headgate.retry._policy import UNRETRYABLE_ERRORS

if TYPE_CHECKING:
    from headgate._responses import RefusedError
    from headgate.retry._policy import RetryOutcome

__all__ = [
    "DEFAULT_REQUEST_ID_HEADERS",
    "MESSAGE_TEXT_LIMIT",
    "SHAPE_ERRORS",
    "CallSite",
    "body_snippet",
    "is_transient_status",
    "loggable_url",
    "mint_error",
    "parse_error",
    "problem_of",
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

#: What httpx2 raises for a response a mint handled out of order (its body read before it was fetched, or twice) or a
#: cookie it asked for by a name two cookies share: the token response could not be read as the mint meant to.
_MISREAD_RESPONSE_ERRORS: tuple[type[Exception], ...] = (httpx2.StreamError, httpx2.CookieConflict)

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

#: The response headers an upstream's request id is read from, first match wins: the de facto names, AWS's, Azure's and
#: Cloudflare's.
DEFAULT_REQUEST_ID_HEADERS: Final = ("x-request-id", "request-id", "x-amzn-requestid", "x-ms-request-id", "cf-ray")

#: A request id longer than this is cut, since it is upstream text that reaches an error.
_REQUEST_ID_LIMIT = 200

#: The largest problem document parsed. A problem detail is a few hundred bytes; past this the body is not one.
_PROBLEM_SCAN_BYTES = 64 * 1024

#: The media type RFC 9457 registers for a JSON problem detail.
_PROBLEM_MEDIA_TYPE = "application/problem+json"

#: What a malformed problem document raises while it is parsed: bad JSON, bad UTF-8, or nesting past the recursion
#: limit.
_PROBLEM_ERRORS = (ValueError, RecursionError)


@dataclass(frozen=True, slots=True)
class CallSite:
    """Where a failure happened: the client, the request line, how many sends the call made, and its request-id headers.

    ``url`` is already sanitized and bounded (:func:`loggable_url`). ``retry`` is the retry layer's verdict on the
    call's last attempt, if it made one. It decides whether the error is retryable (see :meth:`details`), and supplies
    the delay the upstream asked for when the builder has none of its own.
    """

    source: str
    method: str
    url: str
    attempts: int | None = None
    request_id_headers: tuple[str, ...] = DEFAULT_REQUEST_ID_HEADERS
    retry: RetryOutcome | None = None

    def details(
        self,
        resp: httpx2.Response | None = None,
        *,
        failure: Exception | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> ErrorDetails:
        """Return the :class:`~headgate.errors.ErrorDetails` of an error raised here, about *resp* if there is one.

        ``retryable`` is the retry policy's verdict on what failed: *failure* if given, else *resp*, else the call's
        last attempt. So it is ``True`` exactly when the active :class:`~headgate.RetryPolicy` would re-send after this
        outcome: a status in its ``statuses`` or a network failure, and only when sending the request again is safe for
        its method. The *retryable* argument is the fallback for a call no retry layer judged (a token mint on a client
        of its own).
        """
        if self.retry is not None:
            result = failure if failure is not None else resp
            judged = self.retry if result is None else self.retry.about(result)
            retryable = judged.retryable
            retry_after = judged.retry_after if retry_after is None else retry_after
        return ErrorDetails(
            source=self.source,
            method=self.method,
            url=self.url,
            attempts=self.attempts,
            request_id=None if resp is None else self._request_id(resp),
            retryable=retryable,
            retry_after=retry_after,
        )

    def retries_status(self, resp: httpx2.Response) -> bool:
        """Return whether the call's retry policy counts *resp*'s status as a failure to re-send, not an answer.

        That is a status in its ``statuses``, or the server's ``x-should-retry: true``. Method safety plays no part: a
        503 to a POST is a failure the policy would re-send were the POST safe to send again.
        """
        return self.retry is not None and self.retry.about(resp).failure

    def _request_id(self, resp: httpx2.Response) -> str | None:
        """Return the first request-id header *resp* carries, bounded, or ``None``."""
        for name in self.request_id_headers:
            value = resp.headers.get(name, "")
            if value:
                return shorten(value, _REQUEST_ID_LIMIT)
        return None


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
    """Return whether *status* is raised by :func:`transient_status_error`, not the status ladder: 429 or any 5xx.

    Whether it is retryable is the retry policy's call (``RetryPolicy.statuses``), not this function's.
    """
    return status == httpx2.codes.TOO_MANY_REQUESTS or status >= httpx2.codes.INTERNAL_SERVER_ERROR


def problem_of(resp: httpx2.Response) -> Problem | None:
    """Return the RFC 9457 problem detail *resp*'s body carries, or ``None`` when it is not one.

    Only a body declared ``application/problem+json``, no larger than :data:`_PROBLEM_SCAN_BYTES`, holding a JSON object
    is read. A standard member of the wrong JSON type is ignored, as the RFC says; every other member is an extension.
    """
    media_type = resp.headers.get("content-type", "").partition(";")[0].strip().lower()
    content = resp.content
    if media_type != _PROBLEM_MEDIA_TYPE or len(content) > _PROBLEM_SCAN_BYTES:
        return None
    try:
        document = json.loads(content)
    except _PROBLEM_ERRORS:
        return None
    if not isinstance(document, Mapping):
        return None
    members = {str(key): value for key, value in document.items()}
    status = members.pop("status", None)
    return Problem(
        type=_text_member(members.pop("type", None)) or "about:blank",
        title=_text_member(members.pop("title", None)),
        status=status if isinstance(status, int) and not isinstance(status, bool) else None,
        detail=_text_member(members.pop("detail", None)),
        instance=_text_member(members.pop("instance", None)),
        extensions=members,
    )


def _text_member(value: object) -> str | None:
    """Return a problem member's value if it is a JSON string, else ``None``."""
    return value if isinstance(value, str) else None


def _summary(resp: httpx2.Response, problem: Problem | None) -> str | None:
    """Return what a message says of *resp*'s body: the problem's title and detail if it has them, else a snippet.

    Bounded and whitespace-collapsed like :func:`body_snippet`.
    """
    members = () if problem is None else (problem.title, problem.detail)
    named = [text for text in members if text is not None and text.strip()]
    if not named:
        return body_snippet(resp)
    text = " ".join(": ".join(named).split())
    return text if len(text) <= _BODY_SNIPPET_LIMIT else text[:_BODY_SNIPPET_LIMIT] + "…"


def status_error(resp: httpx2.Response, site: CallSite) -> UpstreamStatusError:
    """Build the ``ClientError`` for a non-success status the call did not declare: the status ladder.

    401/403 become :class:`~headgate.errors.AuthError`, because a bad credential is this client's configuration
    failing against the upstream, not the caller's request. Any other status becomes
    :class:`~headgate.errors.UpstreamStatusError`. Both carry the real status, the problem detail if the body is one,
    and a bounded summary of the body.
    """
    kind = AuthError if resp.status_code in _AUTH_STATUSES else UpstreamStatusError
    problem = problem_of(resp)
    return kind(
        f"http error for {site.method} {site.url}",
        status_code=resp.status_code,
        body=_summary(resp, problem),
        problem=problem,
        **site.details(resp),
    )


def refusal_error(refused: RefusedError, resp: httpx2.Response, site: CallSite) -> UpstreamStatusError:
    """Build the ``ClientError`` for a provider's :class:`RefusedError`, from a reader or the host's ``refusal_check``.

    Both paths build the error here, so both carry the provider's code, the client's name and the body snippet.
    ``refused.kind`` names the class, and the status is the one the wire sent: a refusal written inside a 200 body
    carries 200, and a host's key-error redirect carries its 302.
    """
    return refused.kind(
        f"{refused.message} ({site.url})",
        status_code=resp.status_code,
        code=refused.code,
        body=body_snippet(resp),
        **site.details(resp),
    )


def parse_error(exc: Exception, resp: httpx2.Response, site: CallSite) -> ResponseParseError:
    """Build the ``ClientError`` for a 2xx body its reader could not read (one of :data:`SHAPE_ERRORS`).

    The message names the status and the request line, and the reader's diagnosis bounded by :func:`shorten`.
    """
    detail = shorten(str(exc)) or type(exc).__name__
    return ResponseParseError(
        f"unreadable {resp.status_code} body for {site.method} {site.url}: {detail}",
        status_code=resp.status_code,
        content_type=resp.headers.get("content-type"),
        **site.details(resp),
    )


def transport_error(exc: httpx2.HTTPError | OSError, site: CallSite) -> TransportError:
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
    message = f"{site.method} {site.url}: {detail}"
    details = site.details(failure=exc, retryable=not isinstance(exc, UNRETRYABLE_ERRORS))
    if isinstance(exc, httpx2.TimeoutException):
        return ClientTimeoutError(message, **details)
    return TransportError(message, **details)


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


def transient_status_error(resp: httpx2.Response, site: CallSite) -> UpstreamStatusError:
    """Build the ``ClientError`` for a 429 or 5xx that reached the client, past whatever retries the policy made.

    A 429 becomes :class:`~headgate.errors.RateLimitError`, anything else
    :class:`~headgate.errors.UpstreamStatusError`. *resp* carries a bounded head of the body, which becomes the
    summary, as it does for :func:`status_error`. The message calls the status ``transient`` when the retry policy
    counts it as a failure to re-send (:meth:`CallSite.retries_status`), and ``upstream`` otherwise (a 501, a 505). The
    error is retryable when the retry policy would re-send after *resp* (see :meth:`CallSite.details`): a 5xx outside
    its ``statuses`` is not, nor is a 503 on a POST without an idempotency key. A 429's or 503's ``retry-after-ms`` or
    ``Retry-After`` is its ``retry_after``.
    """
    code = resp.status_code
    kind = RateLimitError if code == httpx2.codes.TOO_MANY_REQUESTS else UpstreamStatusError
    problem = problem_of(resp)
    wording = "transient" if site.retries_status(resp) else "upstream"
    return kind(
        f"{wording} {code} for {site.method} {site.url}",
        status_code=code,
        body=_summary(resp, problem),
        problem=problem,
        **site.details(resp, retry_after=retry_after_of(resp)),
    )


def mint_error(exc: Exception, *, source: str) -> Exception:
    """Map a failure raised while a :class:`~headgate.CachedTokenAuth` minted its token onto the ``ClientError`` tree.

    The mint usually runs on a client of its own, so its failure names the token endpoint, never the API URL of the
    call that needed the token, and it is raised from the auth flow rather than the transport, so the API host's
    circuit breaker records nothing for it:

    * a ``ClientError`` is returned as it is;
    * ``httpx2.HTTPStatusError`` (a mint's ``raise_for_status()``) becomes the status ladder's error for the token
      endpoint: ``AuthError`` for 401/403, ``UpstreamStatusError`` otherwise;
    * any other ``httpx2.HTTPError`` or an ``OSError`` becomes ``TransportError`` / ``ClientTimeoutError``;
    * one of :data:`SHAPE_ERRORS` (an unreadable token response), or an ``httpx2.StreamError`` or
      ``httpx2.CookieConflict`` (a response handled out of order, a cookie name two cookies share), becomes
      ``ResponseParseError``.

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
        return kind(
            f"token mint failed for {method} {log_url}",
            status_code=resp.status_code,
            body=body,
            source=source,
            method=method,
            url=log_url,
        )
    if isinstance(exc, httpx2.HTTPError | OSError):
        method, log_url = _request_line(exc)
        return transport_error(exc, CallSite(source, method, f"{log_url} (token mint)"))
    if isinstance(exc, SHAPE_ERRORS + _MISREAD_RESPONSE_ERRORS):
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
