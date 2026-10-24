"""The ``ClientError`` a failed exchange becomes: pure builders.

Each builder RETURNS its error rather than raising it, so the caller raises it with the right cause (``from exc`` for
a transport failure, ``from None`` for a reader's :class:`~headgate._responses.Refused`). Each takes ``source=``,
the client class's name, which every ``ClientError`` carries. A ``log_url`` argument is ALREADY sanitized
(:func:`~headgate._url.sanitize_url`), because every message here reaches the application boundary's 502 detail; only
:func:`transient_status_error`, which reads ``resp.url`` itself, sanitizes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2

from headgate._url import redact_secrets, sanitize_url
from headgate.errors import AuthError, ClientTimeoutError, RateLimitError, TransportError, UpstreamStatusError

if TYPE_CHECKING:
    from headgate._responses import Refused

__all__ = ["body_snippet", "refusal_error", "status_error", "transient_status_error", "transport_error"]

#: Cap on the error-body snippet folded into a ClientError message — enough for a
#: provider's structured reason, not enough to turn an HTML error page into a payload.
_BODY_SNIPPET_LIMIT = 300


def body_snippet(resp: httpx2.Response) -> str | None:
    """A bounded, single-line view of an error response body.

    The upstream's own reason for a non-transient 4xx IS the diagnosis (a 400 that names the
    missing field), so the raise folds it into
    the ``ClientError`` message — which reaches the application boundary's 502 detail, hence
    bounded and whitespace-collapsed rather than verbatim.
    """
    text = " ".join(resp.text.split())
    if not text:
        return None
    if len(text) > _BODY_SNIPPET_LIMIT:
        return text[:_BODY_SNIPPET_LIMIT] + "…"
    return text


def status_error(resp: httpx2.Response, log_url: str, *, source: str) -> UpstreamStatusError:
    """The status ladder: the ``ClientError`` for a non-success status the call did not declare.

    401/403 → :class:`~headgate.errors.AuthError` (a bad credential is OUR configuration failing against the
    upstream, not the caller's request). Any other non-success → :class:`~headgate.errors.UpstreamStatusError`.
    Both carry the real status and a bounded body snippet.
    """
    kind = AuthError if resp.status_code in (401, 403) else UpstreamStatusError
    return kind(f"http error for {log_url}", status_code=resp.status_code, source=source, body=body_snippet(resp))


def refusal_error(refused: Refused, resp: httpx2.Response, log_url: str, *, source: str) -> UpstreamStatusError:
    """The ``ClientError`` for a provider's :class:`Refused`: a reader's, or the host ``refusal=`` check's.

    A reader raises its ``Refused`` and the host check returns one; both paths build the error here, so both carry
    the provider's code, the client's name and the body snippet. ``refused.kind`` names the kind (credential, rate
    limit, other), and the status is the one the wire sent: a refusal written inside a 200 body carries 200, and
    a host's key-error redirect carries its 302.
    """
    return refused.kind(
        f"{refused.message} ({log_url})",
        status_code=resp.status_code,
        code=refused.code,
        source=source,
        body=body_snippet(resp),
    )


def transport_error(exc: httpx2.HTTPError | OSError, method: str, log_url: str, *, source: str) -> TransportError:
    """The ``ClientError`` for a network, protocol or timeout failure: nothing usable came back.

    httpx2 names the URL in most of its own messages but a bare ``OSError`` names nothing, so the request is stated
    explicitly rather than hoped for. Many httpx2 exceptions stringify to "" (a dropped connection, a read timeout),
    and the type name is then the only record of what failed. ``TimeoutException`` is checked first because it IS an
    ``HTTPError``.
    """
    detail = f"{method.upper()} {log_url}: {redact_secrets(str(exc)) or type(exc).__name__}"
    if isinstance(exc, httpx2.TimeoutException):
        return ClientTimeoutError(detail, source=source)
    return TransportError(detail, source=source)


def transient_status_error(resp: httpx2.Response, *, source: str) -> UpstreamStatusError:
    """The ``ClientError`` for a 429 or 5xx that outlasted the retry transport's attempts.

    Rate limit vs generic upstream failure; 401/403 are non-transient and belong to :func:`status_error`. The URL is
    ``resp.url`` put through :func:`~headgate._url.sanitize_url`, not ``resp.url`` itself: providers that
    authenticate by QUERY PARAM would otherwise leak the key, and this message reaches the boundary handler's 502 detail.
    """
    code = resp.status_code
    kind = RateLimitError if code == 429 else UpstreamStatusError
    return kind(f"transient {code} {sanitize_url(str(resp.url))}", status_code=code, source=source)
