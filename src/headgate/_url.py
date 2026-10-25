# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""URL handling: path-segment escaping, and redacting credentials from a URL or a message.

**Escaping.** Interpolating a caller's value straight into an f-string path lets that value control the URL's
structure rather than occupy one segment of it::

    f"/v2/positions/{symbol}"        symbol = "../../v2/account"
      -> httpx2 resolves the dot segments
      -> DELETE https://api.example.test/v2/account

The same gap is an ordinary correctness bug wherever a legitimate identifier contains a slash: an unescaped ``BTC/USD``
builds ``/v2/positions/BTC/USD``, a different endpoint. Escaping belongs in the client because the client is the layer
that builds URLs; every layer above it deals in real, unescaped identifiers (``BTC/USD`` is the symbol), and pushing
escaping up would corrupt the value for display, comparison and the response models. :func:`render_path` renders a
t-string path and escapes every interpolation with :func:`escape_segment`. Query parameters need none of this: httpx2
encodes ``params=`` itself.

**Redaction.** :data:`SECRET_QUERY_PARAMS` names the query parameters that carry a credential, and a ``QueryParamAuth``
adds the names it injects with :func:`register_secret_params`. :func:`sanitize_url` redacts every URL a ``ClientError``
message names, :func:`redact_secrets` redacts ``name=value`` pairs and URL userinfo in free text such as an httpx2
exception message, and the HTTP cache strips the same parameters before it stores a request. A URL's whole userinfo is a
credential, a bare ``user@`` included: a token sent as the username (``https://ghp_…@host``) is how several hosts
take one.
"""

from __future__ import annotations

import re
from string.templatelib import Template
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "SECRET_QUERY_PARAMS",
    "escape_segment",
    "is_secret_param",
    "redact_secrets",
    "register_secret_params",
    "render_path",
    "route_template",
    "sanitize_url",
]

#: Query parameter names that carry a credential, compared after :func:`_fold` (case-insensitive, ``_`` and ``-``
#: ignored).
SECRET_QUERY_PARAMS: Final = frozenset(
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

#: Names added by :func:`register_secret_params`, folded. Process-global and add-only: over-redacting is safe.
_registered_secret_params: set[str] = set()

#: The placeholder that replaces a credential.
_REDACTED: Final = "***"

#: A ``name=value`` pair in free text. The value stops at ``&``, whitespace or a quote, which is where an URL embedded
#: in an exception message ends.
_PAIR: Final = re.compile(r"([A-Za-z][\w-]*)=([^&\s'\"]+)")

#: The userinfo of a URL in free text: everything between ``//`` and an ``@`` before the first ``/``, ``?`` or ``#``.
_USERINFO: Final = re.compile(r"//[^/?#\s@'\"]+@")

#: A value that escapes to nothing but dots is a structural dot segment, not data. See :func:`escape_segment`.
_DOT_SEGMENTS: Final = {".": "%2E", "..": "%2E%2E"}


def _fold(name: str) -> str:
    """Normalize a parameter name for comparison: lower case, with ``_`` and ``-`` removed."""
    return name.lower().replace("_", "").replace("-", "")


def register_secret_params(names: Iterable[str]) -> None:
    """Treat *names* as credential parameters from now on, in every client in the process."""
    _registered_secret_params.update(_fold(n) for n in names)


def is_secret_param(name: str) -> bool:
    """Return whether the query parameter *name* carries a credential."""
    folded = _fold(name)
    return folded in SECRET_QUERY_PARAMS or folded in _registered_secret_params


def redact_secrets(text: str) -> str:
    """Return *text* with every URL userinfo, and the value of every credential ``name=value`` pair, replaced.

    A userinfo (``//user:pw@`` or ``//token@``) becomes ``//***@``; a pair whose name is a credential keeps its name.
    """
    text = _USERINFO.sub(f"//{_REDACTED}@", text)
    return _PAIR.sub(lambda m: f"{m[1]}={_REDACTED}" if is_secret_param(m[1]) else m[0], text)


def escape_segment(value: object) -> str:
    """Escape *value* as exactly one URL path segment.

    *value* is stringified first, since call sites pass ``UUID`` and ``int`` ids as well as ``str``. ``safe=""`` is the
    point: :func:`urllib.parse.quote` keeps ``/`` by default. Everything structural (``/``, ``?``, ``#``, whitespace)
    is percent-encoded, while characters legal inside a segment survive, notably the ``.`` in ``BRK.B``.

    Keeping ``.`` legal means a value made only of dots would reach the URL as a live dot segment, which httpx2
    resolves: ``DELETE /v2/positions/.`` collapses to ``DELETE /v2/positions``, which closes **every** position
    instead of one. So ``.`` and ``..`` map to their percent-encoded forms, which httpx2 does not resolve. The mapping
    applies only when the whole escaped value is dots, so ``BRK.B`` and a literal ``..`` between two holes (a
    ``{start}..{end}`` range) are untouched.

    :raises ValueError: if *value* is empty. ``t"/v2/positions/{symbol}"`` with ``""`` would render
        ``/v2/positions/``, one trailing slash from the collection endpoint, and a router that strips the slash with a
        301 or 308 re-sends the request there. No path segment is legitimately empty.
    """
    escaped = quote(str(value), safe="")
    if not escaped:
        msg = "empty URL path segment"
        raise ValueError(msg)
    return _DOT_SEGMENTS.get(escaped, escaped)


def render_path(template: Template) -> str:
    """Render a t-string URL path, escaping every interpolation with :func:`escape_segment` by default.

    An f-string is one flat ``str`` by the time anything sees it, but a :class:`~string.templatelib.Template` keeps
    its literals and its holes apart, so ``t"/v2/positions/{symbol}"`` yields ``BTC%2FUSD`` with nothing for the
    author to remember.

    Only two things splice verbatim:

    * A nested ``Template``, trusted for what it is rather than what it says, so shared prefixes stay unannotated:
      ``render_path(t"{_PREFIX}/accounts")`` with ``_PREFIX = t"/api/v3"`` gives ``"/api/v3/accounts"``. A nested
      template's own holes are still escaped, so trust attaches to structure, never to the values inside it. A
      ``:raw`` spec on a ``Template`` is ignored, so it cannot launder those values.
    * A value formatted with the reserved spec ``raw`` (``t"/x/{fragment:raw}"``), the one escape hatch for a pre-built
      fragment. It is a spec rather than a second function so that every unescaped interpolation is written ``:raw}``
      at its point of use. A spec can itself be interpolated (``t"/x/{v:{spec}}"`` with ``spec = "raw"``), which
      renders unescaped without matching a search for ``:raw}``.

    Any other ``format_spec`` is applied first and the result escaped, so ``t"/shvol{d:%Y%m%d}.txt"`` reads as its
    f-string counterpart did. Recursion is bounded: ``Template`` is immutable, so a cycle cannot be built.

    :raises TypeError: if *template* is not a ``Template``. A ``str`` would otherwise iterate as literal characters and
        come back unchanged: an f-string's already-interpolated, unescaped text passing silently through.
    :raises ValueError: if an interpolation carries a conversion (``!r``, ``!s``, ``!a``), which a URL segment almost
        never wants and which dropping silently would change what the equivalent f-string did.
    """
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


def route_template(template: Template) -> str:
    """Return the route a t-string URL path names: its literal text with each hole written as ``{expression}``.

    ``t"series/{series_id}/observations"`` gives ``"series/{series_id}/observations"`` whatever the id, so events and
    traces can group calls by endpoint without the values in them. A nested ``Template`` contributes its own route; a
    format spec, ``:raw`` included, is not shown.
    """
    out: list[str] = []
    for part in template:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part.value, Template):
            out.append(route_template(part.value))
        else:
            out.append(f"{{{part.expression}}}")
    return "".join(out)


def _redact_query(query: str) -> str:
    """Return *query* with the value of every credential parameter replaced, and every other byte kept as it was.

    Names are compared decoded, so ``api%5Fkey`` is caught too. The placeholder is written percent-encoded
    (``%2A%2A%2A``), as a query value is.
    """
    placeholder = quote(_REDACTED)
    pairs: list[str] = []
    for pair in query.split("&"):
        name = pair.partition("=")[0]
        pairs.append(f"{name}={placeholder}" if is_secret_param(unquote_plus(name)) else pair)
    return "&".join(pairs)


def _redact_userinfo(netloc: str) -> str:
    """Return *netloc* with its whole userinfo, when it has one, replaced by ``***``."""
    userinfo, _, hostport = netloc.rpartition("@")
    return f"{_REDACTED}@{hostport}" if userinfo else netloc


def sanitize_url(url: str) -> str:
    """Return *url* with its credentials replaced by ``***``; it never raises.

    That covers secret query parameters, the whole userinfo, and secret ``name=value`` pairs in the fragment (an OAuth
    implicit-flow ``#access_token=``). Everything else in the URL is kept byte for byte, so the URL a message names is
    the one that was sent. A URL too malformed to split (an unclosed IPv6 bracket) is redacted as free text with
    :func:`redact_secrets` instead, since its message still has to be safe to show.
    """
    if not any(marker in url for marker in ("?", "#", "@")):
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_secrets(url)
    return urlunsplit(
        parts._replace(
            netloc=_redact_userinfo(parts.netloc),
            query=_redact_query(parts.query),
            fragment=redact_secrets(parts.fragment),
        )
    )
