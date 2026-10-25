# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The ``ClientError`` a failed exchange becomes: pure builders.

Each builder returns its error rather than raising it, so the caller raises it with the right cause (``from exc`` for
a transport failure, ``from None`` for a reader's :class:`~headgate._responses.RefusedError`). Each takes
``source=``, the client class's name, which every ``ClientError`` carries.

Every message here can reach an application boundary's error detail, so no builder puts a raw URL in one. A
``log_url`` argument is already sanitized (:func:`~headgate._url.sanitize_url`); :func:`transient_status_error`, which
reads ``resp.url`` itself, sanitizes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

from headgate._url import redact_secrets, sanitize_url
from headgate.errors import AuthError, ClientTimeoutError, RateLimitError, TransportError, UpstreamStatusError

if TYPE_CHECKING:
    from headgate._responses import RefusedError

__all__ = [
    "body_snippet",
    "is_transient_status",
    "refusal_error",
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

#: The statuses that name our own credential as the failure.
_AUTH_STATUSES = frozenset({httpx2.codes.UNAUTHORIZED, httpx2.codes.FORBIDDEN})


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


def status_error(resp: httpx2.Response, log_url: str, *, source: str) -> UpstreamStatusError:
    """Build the ``ClientError`` for a non-success status the call did not declare: the status ladder.

    401/403 become :class:`~headgate.errors.AuthError`, because a bad credential is this client's configuration
    failing against the upstream, not the caller's request. Any other status becomes
    :class:`~headgate.errors.UpstreamStatusError`. Both carry the real status and a bounded body snippet.
    """
    kind = AuthError if resp.status_code in _AUTH_STATUSES else UpstreamStatusError
    return kind(f"http error for {log_url}", status_code=resp.status_code, source=source, body=body_snippet(resp))


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


def transport_error(exc: httpx2.HTTPError | OSError, method: str, log_url: str, *, source: str) -> TransportError:
    """Build the ``ClientError`` for a network, protocol or timeout failure: nothing usable came back.

    httpx2 names the URL in most of its own messages but a bare ``OSError`` names nothing, so the request line is
    stated explicitly. Many httpx2 exceptions stringify to ``""`` (a dropped connection, a read timeout), and the type
    name is then the only record of what failed. A timeout becomes :class:`~headgate.errors.ClientTimeoutError`.
    """
    detail = f"{method} {log_url}: {redact_secrets(str(exc)) or type(exc).__name__}"
    if isinstance(exc, httpx2.TimeoutException):
        return ClientTimeoutError(detail, source=source)
    return TransportError(detail, source=source)


def transient_status_error(resp: httpx2.Response, *, source: str) -> UpstreamStatusError:
    """Build the ``ClientError`` for a 429 or 5xx that outlasted the retry transport's attempts.

    A 429 becomes :class:`~headgate.errors.RateLimitError`, anything else
    :class:`~headgate.errors.UpstreamStatusError`. The URL is the one that answered, put through
    :func:`~headgate._url.sanitize_url` so a credential sent as a query parameter stays out of the message.
    """
    code = resp.status_code
    kind = RateLimitError if code == httpx2.codes.TOO_MANY_REQUESTS else UpstreamStatusError
    return kind(f"transient {code} {sanitize_url(str(resp.url))}", status_code=code, source=source)
