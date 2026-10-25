# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""URL handling: path-segment escaping, and redacting a credential from a URL.

Two of its functions escape: :func:`render_path` renders a whole t-string URL path, and :func:`escape_segment` — which
it calls — escapes one value as a single segment. They exist because interpolating a caller's value straight into an
f-string
path lets that value control the URL's STRUCTURE rather than occupying one segment of it::

    f"/v2/positions/{symbol}"        symbol = "../../v2/account"
      -> httpx2 resolves the dot segments
      -> DELETE https://api.example.test/v2/account

The same gap is an ordinary correctness bug wherever a legitimate identifier contains a slash: an unescaped ``BTC/USD``
builds ``/v2/positions/BTC/USD``, a different endpoint, which 404s instead of closing the position.

**Escaping belongs in the client** because the client is the layer that builds URLs. Every layer above it deals in real,
unescaped identifiers, and should: ``BTC/USD`` IS the symbol. Pushing escaping up would mean every caller remembering to
pre-encode, and would corrupt the value for display, comparison, and the response models.

Query parameters need none of this — httpx2 encodes ``params=`` itself. This is only for values interpolated into the
PATH.
"""

from __future__ import annotations

import re
from string.templatelib import Template
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Query-string parameter NAMES that carry a credential, compared by :func:`_fold`: case-insensitive, ``_`` and ``-``
#: ignored. Redacted from every URL a ``ClientError`` message names (:func:`sanitize_url`) and stripped before a
#: request reaches the shared HTTP cache (``_cache``), which would otherwise persist the key in plaintext inside the
#: stored request URL. A ``QueryParamAuth`` adds the names it injects through :func:`register_secret_params`.
SECRET_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        # API keys and generic secrets
        "apikey",
        "key",
        "accesskey",
        "privatekey",
        "secret",
        "clientsecret",
        "auth",
        "password",
        "passwd",
        "pwd",
        # tokens (OAuth 2.0, OpenID Connect, sessions)
        "token",
        "accesstoken",
        "authtoken",
        "refreshtoken",
        "idtoken",
        "clientassertion",
        "jwt",
        "session",
        "sessionid",
        "crumb",
        # signatures, including AWS SigV4 and Google Cloud Storage signed URLs
        "sig",
        "signature",
        "xamzsignature",
        "xamzcredential",
        "xamzsecuritytoken",
        "xgoogsignature",
        "xgoogcredential",
    }
)
_registered: set[str] = set()

_PAIR = re.compile(r"([A-Za-z][\w-]*)=([^&\s'\"]+)")

#: A value that escapes to nothing but dots is a *structural* dot segment, not data. See :func:`escape_segment`.
_DOT_SEGMENTS = {".": "%2E", "..": "%2E%2E"}

__all__ = [
    "SECRET_QUERY_PARAMS",
    "escape_segment",
    "is_secret_param",
    "redact_secrets",
    "register_secret_params",
    "render_path",
    "sanitize_url",
]


def _fold(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "")


def register_secret_params(names: Iterable[str]) -> None:
    """Treat *names* as credential parameters from now on. Process-global and add-only: over-redacting is safe."""
    _registered.update(_fold(n) for n in names)


def is_secret_param(name: str) -> bool:
    """Whether the query parameter *name* carries a credential."""
    folded = _fold(name)
    return folded in SECRET_QUERY_PARAMS or folded in _registered


def redact_secrets(text: str) -> str:
    """*text* with the value of every ``name=value`` pair whose name is a credential parameter replaced by ``***``."""
    return _PAIR.sub(lambda m: f"{m[1]}=***" if is_secret_param(m[1]) else m[0], text)


def escape_segment(value: object) -> str:
    """Escape *value* as exactly ONE URL path segment.

    ``safe=""`` is the whole point: :func:`urllib.parse.quote` leaves ``/`` alone by default, which would defeat the
    purpose here. Everything structural (``/``, ``?``, ``#``, whitespace) is percent-encoded; the characters that are
    legal inside a segment and load-bearing in real tickers — notably ``.`` in ``BRK.B`` — survive untouched.

    **The dots are the exception, and they have to be.** Keeping ``.`` legal for ``BRK.B`` means a value that is
    ENTIRELY dots reaches the URL as a live dot segment — structure, not data — and httpx2 resolves it::

        escape_segment(".")   -> "."    DELETE /v2/positions/.      collapses to  DELETE /v2/positions
        escape_segment("..")  -> ".."   DELETE /v2/positions/..     collapses to  DELETE /v2

    That first line is the dangerous one: ``DELETE /v2/positions/{symbol}`` closes ONE position, while
    ``DELETE /v2/positions`` liquidates EVERY position. So the two are mapped to their percent-encoded forms, which
    httpx2 does NOT resolve. The special case fires only when the whole escaped value is dots, so ``BRK.B`` and a
    literal ``..`` written BETWEEN two holes (a ``{escape_segment(start)}..{end}`` date range) are untouched.

    Accepts any object and stringifies it, since call sites pass ``UUID`` and ``int`` ids as well as ``str``.
    """
    escaped = quote(str(value), safe="")
    if not escaped:
        # An EMPTY segment is the dot-segment hazard's quieter twin: ``t"/v2/positions/{symbol}"`` with
        # ``""`` renders ``DELETE /v2/positions/``, one trailing slash from the liquidate-everything endpoint, and a
        # router that strips the slash with a 301/308 re-sends the DELETE there. No path segment is legitimately empty.
        msg = "empty URL path segment"
        raise ValueError(msg)
    return _DOT_SEGMENTS.get(escaped, escaped)


def render_path(template: Template) -> str:
    """Render a t-string URL path, escaping every interpolation BY DEFAULT.

    :func:`escape_segment` is a convention and works only where the author remembered it. This is the mechanism that
    replaces it: an f-string is already one flat ``str`` by the time anything sees it, but a
    :class:`~string.templatelib.Template` keeps its literals and its holes apart, so the escaping decision is made
    HERE. ``t"/v2/positions/{symbol}"`` yields ``BTC%2FUSD`` with the author doing nothing — there is nothing to
    forget.

    **Trust is a type.** The one thing that splices verbatim is a nested ``Template``, trusted for what it *is*
    rather than what it says, which is what lets shared path prefixes stay unannotated::

        _PREFIX = t"/api/v3/brokerage"
        render_path(t"{_PREFIX}/accounts")  ->  "/api/v3/brokerage/accounts"

    That is a boundary argument, not a proof: ``Template`` is constructible by hand, but data crossing a boundary
    arrives as ``str``, ``bytes`` or parsed JSON, and none of those becomes one without someone deliberately wrapping
    it — visibly, at the site that did it. Splicing recurses but does not launder: a nested template's own holes are
    escaped either way, so trust attaches to structure and never to the values inside it.

    ``format_spec`` is applied FIRST and the result escaped, so ordinary formatting call sites
    (``t"/shvol{d:%Y%m%d}.txt"``) read exactly as their f-string predecessors did. ``raw`` is the single reserved
    spec and the only escape hatch for a pre-built fragment, deliberately a *spec* rather than a second function so
    that every unescaped interpolation of a non-``Template`` is written ``:raw}`` at the point of use.

    **That greppability is narrower than it looks, in two ways.** A format spec may itself be interpolated and is
    evaluated eagerly, so ``t"/x/{v:{spec}}"`` with ``spec = "raw"`` renders UNESCAPED while matching no grep for
    ``:raw}``, and this function cannot tell. A nested ``Template`` also splices unescaped and greps as neither; that
    one is by design.

    ``raw`` is IGNORED on a ``Template``: the ``Template`` branch is tested first, so ``t"{inner:raw}"`` recurses
    exactly as ``t"{inner}"`` does. Of the two readings that is the safer one — the alternative would let ``:raw`` on
    a wrapper launder every value inside it.

    Recursion is not a denial-of-service surface: ``Template`` is immutable and unsubclassable, so a cycle cannot be
    constructed, and real prefixes nest two or three deep against a limit of ~1000.

    A ``conversion`` (``!r`` / ``!s`` / ``!a``) raises rather than being silently dropped — ``!r`` on a ``str`` adds
    quotes a URL segment almost never wants, and ignoring it would silently change what the f-string it replaced did.
    Values are stringified as :func:`escape_segment` does, since call sites pass ``UUID`` and ``int`` ids too.

    :raises TypeError: if *template* is not a :class:`~string.templatelib.Template`.
    :raises ValueError: if any interpolation carries a ``conversion``.
    """
    # Not a belt-and-braces isinstance: iterating a ``str`` yields characters, every one of which takes the literal
    # branch, so ``render_path(f"/v2/positions/{sym}")`` would otherwise return the f-string's ALREADY-INTERPOLATED,
    # UNESCAPED text — a silent no-op on a security primitive. Static checks catch it; this makes it loud anyway.
    if not isinstance(template, Template):
        msg = f"render_path() takes a t-string Template, got {type(template).__name__}"
        raise TypeError(msg)

    out: list[str] = []
    for part in template:
        if isinstance(part, str):
            out.append(part)
            continue
        if part.conversion is not None:
            msg = f"conversion !{part.conversion} is not supported in a URL path: {part.expression!r}"
            raise ValueError(msg)
        if isinstance(part.value, Template):
            out.append(render_path(part.value))
        elif part.format_spec == "raw":
            out.append(str(part.value))
        else:
            value = format(part.value, part.format_spec) if part.format_spec else str(part.value)
            out.append(escape_segment(value))
    return "".join(out)


def sanitize_url(url: str) -> str:
    """Return *url* with its credentials replaced by ``***``.

    That is secret query parameters, a userinfo password, and secret ``name=value`` pairs in the fragment (an OAuth
    implicit-flow ``#access_token=``).

    Every URL a ``ClientError`` message names goes through here first, because those messages may reach an API
    response or a log, and some providers authenticate by query parameter. A URL with none of these is returned
    unchanged.
    """
    if not any(marker in url for marker in ("?", "#", "@")):
        return url
    parsed = urlparse(url)
    if parsed.query:
        qs = parse_qs(parsed.query, keep_blank_values=True)
        redacted = {k: (["***"] if is_secret_param(k) else v) for k, v in qs.items()}
        parsed = parsed._replace(query=urlencode(redacted, doseq=True))
    if parsed.password:
        parsed = parsed._replace(netloc=parsed.netloc.replace(f":{parsed.password}@", ":***@", 1))
    if parsed.fragment:
        parsed = parsed._replace(fragment=redact_secrets(parsed.fragment))
    return urlunparse(parsed)
