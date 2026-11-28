# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Provider-agnostic ``httpx2.Auth`` classes: static headers, query parameters, per-request signatures, minted tokens.

``httpx2.AsyncClient`` runs ``auth.async_auth_flow(request)`` once per logical request, above the transport stack, so a
re-send by the retry transport repeats the credential the flow stamped. :class:`SigningAuth` is the exception: its flow
only marks the request (:data:`SIGNER_EXT`), and the transport stack signs every attempt inside the retry layer, so a
retry or a hedge carries a fresh signature. A provider's own signer (a JWT signed with a vendor key, an HMAC over the
request) belongs in that provider's client, as a :class:`SigningAuth` subclass.

**An explicit ``headers=`` or ``params=`` from the caller wins a clash.** Every class here merges setdefault-style,
because the flow runs at send time and a plain assignment would override the caller.

Every class here is an :class:`Auth`, the one type ``Client(auth=)`` takes: it declares the headers and query
parameters it stamps (``credential_headers``, ``credential_params``), so the client withholds them from any origin it
does not trust (:mod:`headgate.security`), and it names the credential it carries (``credential_identity()``), so a
cache can keep one credential's responses from another's. The identity is the secret material itself; it is hashed,
never logged or stored.

Only the async flow is implemented; the clients are async-only.
"""

import asyncio
import copy
import inspect
import math
import secrets
import threading
import weakref
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Iterable, Mapping
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, ClassVar, Final, cast, final, get_args, get_origin, override

import httpx2

from headgate._body import read_capped
from headgate._core import (
    CallContext,
    Clock,
    Slot,
    Timing,
    check_default_headers,
    check_kind,
    check_number,
    check_str_mapping,
    current_timing,
    named,
    origin_text,
    sealed,
    type_text,
)
from headgate._errors import ClientError
from headgate._failures import CallSite, loggable_url, mint_error, shorten, transport_error
from headgate._url import redact_secrets, register_secret_params
from headgate.errors import ClientConfigurationError
from headgate.retry._policy import REPLAYABLE_BODY
from headgate.security._credentials import CHAIN_ORIGIN, check_declarations

__all__ = [
    "AUTH_HOP",
    "FRESH_TOKEN_S",
    "MINTING",
    "SIGNER_EXT",
    "Auth",
    "BearerAuth",
    "CachedTokenAuth",
    "Minted",
    "QueryParamAuth",
    "SigningAuth",
    "StaticHeaderAuth",
    "check_timing",
    "failed_on_auth_hop",
    "hold_timing",
    "mint_hop_failure",
    "refuse_auth_headers",
    "refuse_undeclared",
    "stamped_names",
    "was_checked",
]

#: The request extension marking a request an auth flow yields to acquire a credential, as opposed to the call's own
#: request. :class:`CachedTokenAuth` sets it on every request its :meth:`~CachedTokenAuth.mint` yields. A fixture
#: recorder can read it to skip the hop, which is not the endpoint's answer.
AUTH_HOP = "headgate.auth_hop"

#: The request extension naming the auth class whose mint flow yielded the request, for :func:`mint_hop_failure`.
_MINTED_BY: Final = "headgate.minted_by"

#: True on a call's context while its :class:`CachedTokenAuth` waits for the mint lock or mints under it, so the call's
#: deadline passing then is not charged to the API origin's breaker: the time went to the credential host. A raise or
#: cancellation leaves it set, for the exchange to read on the way out.
MINTING = Slot[bool]("minting")

#: The request extension holding the :class:`SigningAuth` that signs the request. Its flow sets it on a request it
#: signs, and the transport stack calls its :meth:`~SigningAuth.sign` on every attempt, after the rate-limit wait and
#: before the credential guard, when it is the client's own ``auth``.
#: httpx2 hands a request's extensions to the redirects that follow it, so each hop is signed for its own origin, and
#: the guard withholds the signature from one it does not trust.
SIGNER_EXT: Final = "headgate.signer"

#: The most of a mint flow's hop response that is read, in decompressed bytes. A token response is a few hundred bytes,
#: and a cookie-plus-crumb page a few dozen KiB; past this the hop is refused as ``ResponseTooLargeError``.
_MAX_MINT_RESPONSE_BYTES = 1024 * 1024

#: A token rejected within this many seconds of being minted is not re-minted: it is not stale, so a fresh one carries
#: the same authority and is rejected the same way. See :class:`CachedTokenAuth`.
FRESH_TOKEN_S = 60.0


@dataclass(frozen=True, slots=True, kw_only=True)
class _Held[T]:
    """A cached token, with when it was minted, is due for a refresh and expires: instants on the auth's clock.

    A monotonic clock is immune to wall-clock steps. ``minted_at`` is ``-inf`` for a token whose age is unknown.
    """

    token: T
    minted_at: float
    due_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Failed:
    """A mint that failed: the number it was started as, and what it raised, copied as it was raised."""

    attempt: int
    error: ClientError

    def raised(self) -> ClientError:
        """Return a copy of the error for one more call to raise: its own traceback, and notes of its own."""
        return _copied(self.error)


def _copied(error: ClientError) -> ClientError:
    """Return a copy of *error* that shares no traceback and no notes list with it."""
    copied = copy.copy(error).with_traceback(None)
    copied.__notes__ = list(getattr(error, "__notes__", ()))
    return copied


async def _buffer_unless_headgate(request: httpx2.Request, context: CallContext | None) -> None:
    """Buffer *request*'s body for a 401's re-send when it is sent by a bare httpx2 client (no *context*).

    A headgate client marks a body that cannot be sent twice (``REPLAYABLE_BODY``), and the flow re-sends only one that
    can, so its uploads stream; a bare client says nothing, so its body is read into memory first. httpx2 honors
    ``requires_request_body`` only in its default flows, which :class:`CachedTokenAuth` replaces.
    """
    if context is None:
        await request.aread()


def _mark_minting(context: CallContext | None, *, minting: bool) -> None:
    """Record on *context*, when the request has one, whether its auth is waiting for or running a mint."""
    if context is not None:
        context.put(MINTING, value=minting)


async def _drop_body(response: httpx2.Response) -> None:
    """Close the body of a response an auth flow answers with a re-send, and leave it empty.

    httpx2 reads in full each response a flow answers with another request, below ``max_response_bytes``, to keep it in
    the call's history; nothing reads a re-sent 401's body, so it is closed unread, as a followed redirect's is.
    """
    # Closed through a response of its own, so *response* is not marked closed: httpx2 still reads it for the history.
    await httpx2.Response(response.status_code, stream=response.stream).aclose()
    response.stream = httpx2.ByteStream(b"")


def _identity(pairs: Iterable[tuple[str, str]]) -> bytes:
    """Encode name-value *pairs* as unambiguous bytes, in sorted order: each part length-prefixed, as a netstring."""
    encoded = bytearray()
    for part in (part for pair in sorted(pairs) for part in pair):
        raw = part.encode()
        encoded += b"%d:%b," % (len(raw), raw)
    return bytes(encoded)


def mint_hop_failure(exc: httpx2.HTTPError | OSError) -> ClientError | None:
    """Return the ``ClientError`` a network failure on a mint flow's hop means, or ``None`` for any other failure.

    httpx2 raises a hop's network failure outside the auth flow, so the client maps it here: as the token endpoint's
    failure, named for the auth class and the hop's request, never the API call's.
    """
    if not (isinstance(exc, httpx2.RequestError) and failed_on_auth_hop(exc)):
        return None
    request = exc.request
    source = str(request.extensions.get(_MINTED_BY, "token mint"))
    return transport_error(exc, CallSite(source, request.method, f"{loggable_url(str(request.url))} (token mint)"))


def failed_on_auth_hop(exc: BaseException) -> bool:
    """Return whether *exc* was raised by a request an auth flow yielded (see :data:`AUTH_HOP`), not the call's own.

    httpx2 stamps the failing request on every ``RequestError`` it raises, so the marker survives to the catch site. The
    circuit breaker uses this to charge such a failure to nobody: a credential host being unreachable says nothing
    about the host the call was addressed to.
    """
    if not isinstance(exc, httpx2.RequestError):
        return False
    try:
        request = exc.request
    except RuntimeError:  # httpx2 raises when the error was built without a request, as a transport double may do
        return False
    return bool(request.extensions.get(AUTH_HOP))


class _Recorded(httpx2.AsyncByteStream):
    """Pass a response's raw body through, keeping a copy of each chunk, so the body can be replayed once it is read."""

    def __init__(self, raw: AsyncGenerator[bytes]) -> None:
        """Pass *raw*'s chunks through."""
        self._raw = raw
        self.chunks: list[bytes] = []

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._raw:
            self.chunks.append(chunk)
            yield chunk

    @override
    async def aclose(self) -> None:
        """Close the raw body, and so the response it comes from."""
        await self._raw.aclose()


async def _read_hop(response: httpx2.Response, source: str) -> httpx2.Response:
    """Read the response to a mint flow's hop, up to 1 MiB, and return it read.

    httpx2 hands an auth flow each response unread, and reads it itself only after the flow has answered, so the flow
    would see ``ResponseNotRead`` on ``.content`` or ``.json()``. The body is read through a copy that counts the
    decompressed bytes (:func:`~headgate._body.read_capped`), and *response* is then given its raw bytes back to read
    for itself: httpx2 reads the same response again to keep it in the call's history. Past the cap the read raises
    ``ResponseTooLargeError``, naming the token mint, so a hostile token host cannot make the flow buffer without end.
    """
    request = response.request
    site = CallSite(source, request.method, f"{loggable_url(str(request.url))} (token mint)")
    network = httpx2.Response(response.status_code, stream=response.stream)
    raw = _Recorded(network.aiter_raw())
    decoded = httpx2.Response(response.status_code, headers=response.headers, stream=raw, request=request)
    try:
        await read_capped(decoded, _MAX_MINT_RESPONSE_BYTES, site)
    finally:
        # Closed whether it was read or refused unread (by its Content-Length), so its pooled connection goes back.
        await network.aclose()
        response.stream = httpx2.ByteStream(b"".join(raw.chunks))
    await response.aread()
    return response


def _prepare_hop(hop: httpx2.Request, call: httpx2.Request, source: str) -> None:
    """Mark *hop*, a request a mint flow yielded for *call*, as *source*'s auth hop, and give it the call's timeouts.

    The marks are :data:`AUTH_HOP` and the :data:`~headgate.security.CHAIN_ORIGIN` the credential guard trusts for the
    hop. A hop that sets no ``timeout`` extension of its own takes the call's: a request built by hand has none, and a
    token host that never answered would hold the call, and the mint's lock, until the deadline. Either way the hop's
    timeouts are cut to the time the call's deadline leaves.
    """
    extensions = {**hop.extensions, AUTH_HOP: True, _MINTED_BY: source, CHAIN_ORIGIN: origin_text(hop.url.origin)}
    timeout = call.extensions.get("timeout")
    if "timeout" not in extensions and isinstance(timeout, dict):
        extensions["timeout"] = dict(timeout)
    hop.extensions = extensions
    context = CallContext.find(call)
    if context is not None:
        context.fit_timeouts(hop)


class Auth(httpx2.Auth, ABC):
    """An ``httpx2.Auth`` that declares what it stamps: the one type ``Client(auth=)`` takes.

    It is the :class:`~headgate.security.CredentialStamper` contract made a base class, so its three members are
    abstract here. It is a ``CredentialStamper`` by its shape, not by deriving from the protocol, which would put the
    protocol's ``(*args, **kwargs)`` constructor in every subclass's place and shadow a sibling base's.

    A subclass declares the headers and query parameters its flow sets, in :attr:`credential_headers` and
    :attr:`credential_params` (a ``frozenset`` of names each, as class attributes or properties, empty when it stamps
    none of that kind), and names the credential they stand for in :meth:`credential_identity`. The client withholds
    exactly the declared names from any origin it does not trust, and the cache keeps them out of storage and keeps one
    credential's entries from another's. A plain ``httpx2.Auth`` declares nothing, so ``Client()`` refuses it: a
    third-party auth is wrapped in a subclass that declares its names, and a fixed header is a
    :class:`StaticHeaderAuth`. A subclass that leaves a member out cannot be built, and one whose declaration is a
    class attribute is checked when it is defined: each must be a ``frozenset`` of names, not all of them one character
    long, as ``frozenset("X-Api-Key")`` is. A declaration through a property is checked when a client is built.
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a declaration given as a class attribute that is not a frozenset of names, or splits a name.

        Refuse an ``auth_flow`` override too, below one of headgate's own ``async_auth_flow``: a client runs only
        ``async_auth_flow``, and headgate's never calls ``auth_flow``. An ``async_auth_flow`` of your own may drive
        ``auth_flow``, so one below it is yours to override. It marks *cls* as checked, so a class below a base
        whose ``__init_subclass__`` skips ``super()``, and so escapes every check here and in ``SigningAuth`` and
        ``CachedTokenAuth``, is refused by ``Client()``.
        """
        super().__init_subclass__(**kwargs)
        declared = {name: getattr(cls, name) for name in ("credential_headers", "credential_params")}
        # A property (or other descriptor) reads per instance, so the client checks it when it is built.
        check_declarations(
            cls.__name__, **{name: value for name, value in declared.items() if not _is_descriptor(value)}
        )
        flow = next(klass for klass in cls.__mro__ if "async_auth_flow" in vars(klass))
        headgates = flow.__module__ == "headgate" or flow.__module__.startswith("headgate.")
        if cls.auth_flow is not httpx2.Auth.auth_flow and headgates:
            msg = (
                f"{cls.__name__} overrides auth_flow, which {flow.__name__}'s async_auth_flow, the flow a client runs, "
                "never calls: a header computed per request belongs in a SigningAuth's sign()"
            )
            raise TypeError(msg)
        setattr(cls, _CHECKED, True)

    @property
    @abstractmethod
    def credential_headers(self) -> frozenset[str]:
        """The headers this auth sets (compared case-insensitively)."""

    @property
    @abstractmethod
    def credential_params(self) -> frozenset[str]:
        """The query parameters this auth sets."""

    @abstractmethod
    def credential_identity(self) -> bytes:
        """Return stable bytes naming the credential (its secret material); never stored, only hashed."""


#: The class attribute :meth:`Auth.__init_subclass__` sets on each class it checks, read from the class's own namespace.
_CHECKED: Final = "_headgate_checked"


def was_checked(auth: Auth) -> bool:
    """Return whether *auth*'s own class went through :meth:`Auth.__init_subclass__` when it was defined."""
    return vars(type(auth)).get(_CHECKED) is True


def _is_descriptor(value: object) -> bool:
    """Return whether *value*, read off a class, is a descriptor such as a property, not a declared value."""
    return hasattr(type(value), "__get__")


#: The two declarations every auth makes.
_DECLARATIONS: Final = ("credential_headers", "credential_params")


def _declares_names(cls: type) -> bool:
    """Return whether *cls* declares a header or parameter, and register the parameters as secret, for redaction.

    :raises TypeError: for a declaration through a property: a ``SigningAuth`` or ``CachedTokenAuth`` declares in
        class attributes, which are checked when the class is defined.
    """
    headers, params = (getattr(cls, name) for name in _DECLARATIONS)
    for name, value in zip(_DECLARATIONS, (headers, params), strict=True):
        # Auth.__init_subclass__ refused any other class attribute that is not a frozenset of names.
        if not isinstance(value, frozenset):
            msg = (
                f"{cls.__name__}.{name} is declared through a property: declare it as a class attribute, such as "
                f"{name} = frozenset({{'X-Signature'}}), which is checked when the class is defined"
            )
            raise TypeError(msg)
    register_secret_params(params)
    return bool(headers or params)


def _refuse_final_overrides(cls: type, base: type, hooks: str) -> None:
    """Refuse *cls* overriding a member *base* marks ``@final``, as ty does, naming its *hooks*.

    The flow holds the guarantees the hooks rely on (every attempt signed, one re-send on a 401), and an override of it
    failed deep inside the call. A class of both ``SigningAuth`` and ``CachedTokenAuth`` is refused first, in its own
    words: it inherits one base's flow, which the other's check would take for an override it never wrote.
    """
    if issubclass(cls, SigningAuth) and issubclass(cls, CachedTokenAuth):
        msg = (
            f"{cls.__name__} subclasses CachedTokenAuth and SigningAuth, which cannot be combined in one auth: each "
            "runs a flow of its own. Subclass CachedTokenAuth for the token, and sign each attempt in a middleware, "
            "which sees the token on the request and declares what it sets"
        )
        raise TypeError(msg)
    for name, member in vars(base).items():
        if getattr(member, "__final__", False) and getattr(cls, name) is not member:
            msg = (
                f"{cls.__name__} overrides {name}, which {base.__name__} runs itself: its hooks are {hooks}, and a "
                "per-request header belongs in a middleware"
            )
            raise TypeError(msg)


#: How each synchronous hook is written, for the refusal of a coroutine function in its place.
_SYNC_HOOKS: Final = {
    "sign": "def sign(self, request, now)",
    "skips": "def skips(self, request)",
    "attach": "def attach(self, request, token)",
}


def _refuse_async_hooks(cls: type, base: type, *hooks: str) -> None:
    """Refuse *cls* when one of its *hooks* is a coroutine function: *base*'s flow calls each and never awaits it.

    An ``async def sign`` or ``attach`` would set nothing, and an ``async def skips`` returns a coroutine, which is
    truthy, so every request would be skipped. ty reports each; this refuses them for an untyped caller too.
    """
    for hook in hooks:
        if inspect.iscoroutinefunction(getattr(cls, hook)):
            msg = (
                f"{cls.__name__}.{hook} is a coroutine function, which {base.__name__} calls without awaiting, so it "
                f"would never run: write {_SYNC_HOOKS[hook]}, and fetch anything slow it needs before the call"
            )
            raise TypeError(msg)


def _check_mint(cls: type[CachedTokenAuth[Any]]) -> None:
    """Refuse a ``mint`` *cls* defines that is not an async generator function, which the flow alone can drive."""
    mint = cls.mint
    if getattr(mint, "__isabstractmethod__", False) or inspect.isasyncgenfunction(mint):
        return
    if inspect.iscoroutinefunction(mint):
        kind = "coroutine function"
    elif inspect.isgeneratorfunction(mint):
        kind = "generator function"
    else:
        kind = "plain function"
    msg = (
        f"{cls.__name__}.mint is a {kind}: write async def mint(self, request): ... yield Minted(token, lifetime), "
        "an async generator that yields each request the exchange needs and ends with the Minted"
    )
    raise TypeError(msg)


#: What a request carries before a hook stamps it: its header names (lower case, as httpx2 keys them) and its raw query.
type Stamped = tuple[frozenset[str], bytes]


def stamped_names(request: httpx2.Request) -> Stamped:
    """Return what :func:`refuse_undeclared` compares *request* against after a hook stamps it."""
    return frozenset(request.headers.keys()), request.url.query


def refuse_undeclared(auth: Auth, hook: str, request: httpx2.Request, before: Stamped) -> None:
    """Raise ``TypeError`` when *hook* set a header or query parameter on *request* that *auth* does not declare.

    *before* is :func:`stamped_names` of the request before the hook ran. The guard withholds only what is declared,
    so an undeclared name would follow the request to any origin; it is refused before anything is sent. Header names
    are compared case-insensitively. The query is parsed only when the hook changed it, since this runs on every
    attempt.
    """
    headers, query = before
    added = frozenset(request.headers.keys()) - headers
    undeclared = set(added - {name.lower() for name in auth.credential_headers}) if added else set[str]()
    if request.url.query != query:
        params = frozenset(request.url.params) - frozenset(httpx2.QueryParams(query))
        undeclared |= params - auth.credential_params
    if undeclared:
        msg = (
            f"{type(auth).__name__}.{hook} set {', '.join(sorted(undeclared))}, which it does not declare, so the "
            "client could not withhold it from an origin it does not trust: add it to credential_headers (a header) "
            "or credential_params (a query parameter)"
        )
        raise TypeError(msg)


def refuse_moved(auth: Auth, hook: str, request: httpx2.Request, origin: httpx2.Origin) -> None:
    """Raise ``TypeError`` when *hook* moved *request* off *origin*, the one it was vetted for and is signed to reach.

    A signature authenticates where a request goes, and the SSRF guard vetted that origin before *hook* ran; a request
    sent elsewhere would carry the signature, and the credentials, to a host nothing checked. A URL is changed in a
    middleware, which runs before the guard.
    """
    moved = httpx2.Origin(request.url)
    if moved != origin:
        msg = (
            f"{type(auth).__name__}.{hook} moved the request from {origin_text(origin)} to {origin_text(moved)}: a "
            "signer signs the request where it goes, so change the URL in a middleware, which runs before the checks"
        )
        raise TypeError(msg)


def _carries(auth: Auth, request: httpx2.Request) -> bool:
    """Return whether *request* carries any of the headers or query parameters *auth* declares."""
    params = request.url.params
    return any(name in request.headers for name in auth.credential_headers) or any(
        name in params for name in auth.credential_params
    )


def refuse_auth_headers(auth: Auth | None, headers: Iterable[str], *, option: str) -> None:
    """Refuse default *headers*, given as *option*, that name a header *auth* declares (compared case-insensitively).

    Every request would carry it before the auth ran, and a request carrying one of the auth's names is the caller's
    own credential to every auth here (:meth:`SigningAuth.skips`, a static auth's ``setdefault``), so the auth would
    never send its own and a 401 would not be re-sent. A call's own ``headers=`` still sends the caller's.

    :raises ValueError: naming *option*, the headers and the auth's class.
    """
    if auth is None:
        return
    declared = {name.lower() for name in auth.credential_headers}
    if clash := [name for name in headers if name.lower() in declared]:
        kind = type(auth).__name__
        msg = (
            f"{option} names {', '.join(clash)}, which {kind} sets: every request would carry it as the caller's own "
            f"credential, so {kind} would never send its own; leave the header to the auth, or set it on a call's "
            "headers= to send the caller's"
        )
        raise ValueError(msg)


#: The scheme :class:`BearerAuth` sends its token under.
_SCHEME: Final = "Bearer"


def _refuse_empty(auth: Auth, stamped: Mapping[str, str], what: str) -> None:
    """Refuse a static *auth* given no *what* to stamp: ``auth=None`` is the client without a credential."""
    if not stamped:
        msg = f"{type(auth).__name__} needs at least one {what} to stamp: pass auth=None for a client without a key"
        raise ValueError(msg)


class StaticHeaderAuth(Auth):
    """Stamp a fixed set of headers onto every request, without overriding a header the caller set.

    Covers the static API key and static bearer cases (an ``Authorization: Bearer`` or ``x-api-key`` header). It stamps
    at least one header: a client with no key configured is built with ``auth=None``.
    """

    def __init__(self, headers: Mapping[str, str]) -> None:
        """Copy *headers*, so a later change to the caller's mapping does not reach the auth.

        :raises TypeError: for *headers* that do not map names to ``str`` values, such as a list of pairs.
        :raises ValueError: for no headers, which would stamp nothing: pass ``auth=None`` for a client without a key;
            for a header named more than once, in any case; and for a name that is not an HTTP token, or a value holding
            CR, LF or NUL, which no request can carry.
        """
        check_default_headers("headers", headers)
        _refuse_empty(self, headers, "header")
        self._headers = dict(headers)

    @property
    @override
    def credential_headers(self) -> frozenset[str]:
        """The headers this auth stamps, lower-cased: withheld from any origin the client does not trust."""
        return frozenset(name.lower() for name in self._headers)

    @property
    @override
    def credential_params(self) -> frozenset[str]:
        """No query parameters: this auth stamps headers only."""
        return frozenset[str]()

    @override
    def credential_identity(self) -> bytes:
        """Return the headers and their values as bytes, names lower-cased: the same headers give the same bytes."""
        return _identity((name.lower(), value) for name, value in self._headers.items())

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        for key, value in self._headers.items():
            request.headers.setdefault(key, value)
        yield request


class BearerAuth(StaticHeaderAuth):
    """Send a static bearer token as ``Authorization: Bearer <token>``: the most common :class:`StaticHeaderAuth`."""

    def __init__(self, token: str) -> None:
        """Send *token* on every request to a trusted origin.

        :raises TypeError: for a *token* that is not a ``str``.
        :raises ValueError: for an empty or blank *token* (an unset environment variable, say), which would send
            ``Bearer `` alone; for one that is ``bearer``, in any case, or starts with it and whitespace, the scheme it
            adds itself; and for one with whitespace around it. No message shows the token.
        """
        check_kind("token", token, str, "the token, a str")
        stripped = token.strip()
        if not stripped:
            msg = "BearerAuth(token=) takes a token that is not empty or blank: is the variable holding it set?"
            raise ValueError(msg)
        # The scheme as a word: alone, or before whitespace; "bearertoken" is a token of its own.
        if stripped.split(maxsplit=1)[0].lower() == _SCHEME.lower():
            msg = "BearerAuth(token=) takes the token alone, without its 'Bearer' scheme, which it adds itself"
            raise ValueError(msg)
        if stripped != token:
            msg = "BearerAuth(token=) takes a token without whitespace around it: strip the value it was read from"
            raise ValueError(msg)
        super().__init__({"Authorization": f"{_SCHEME} {token}"})


class QueryParamAuth(Auth):
    """Merge a fixed set of query params into the request URL, without overriding a param the caller set.

    **The key rides the URL, and URLs are not secret to observability.** A tracing integration that records the full
    request URL (OpenTelemetry's httpx instrumentation, for one) records the key in cleartext, so exclude the host of
    any client using this class from URL capture. Error messages and the HTTP cache are safe regardless: the
    well-known secret parameter names and the names given here are redacted from both. The names are registered
    process-wide, which only ever over-redacts.
    """

    def __init__(self, params: Mapping[str, str]) -> None:
        """Copy *params* and register their names as secret, for redaction from error messages and the cache.

        :raises TypeError: for *params* that do not map names to ``str`` values, such as a bare ``str``.
        :raises ValueError: for no parameters, which would stamp nothing: pass ``auth=None`` for a client without a key.
        """
        check_str_mapping("params", params, "a mapping of parameter names to str values, such as {'api_key': key}")
        _refuse_empty(self, params, "parameter")
        self._params = dict(params)
        register_secret_params(self._params)

    @property
    @override
    def credential_headers(self) -> frozenset[str]:
        """No headers: this auth stamps query parameters only."""
        return frozenset[str]()

    @property
    @override
    def credential_params(self) -> frozenset[str]:
        """The query parameters this auth stamps: withheld from any origin the client does not trust."""
        return frozenset(self._params)

    @override
    def credential_identity(self) -> bytes:
        """Return the parameters and their values as bytes: the same parameters give the same bytes."""
        return _identity(self._params.items())

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        existing = request.url.params
        additions = {k: v for k, v in self._params.items() if k not in existing}
        if additions:
            request.url = request.url.copy_merge_params(additions)
        yield request


class SigningAuth(Auth):
    """Sign every attempt of a request with a provider's signer, and send it once more on a 401.

    For a credential that is a signature over the request itself, made fresh for every request: a JWT bound to the
    method and path, an HMAC or Ed25519 signature over a timestamp and the body. The signer is the subclass's
    :meth:`sign`; the base class runs the flow around it:

    * **Every attempt is signed afresh**, inside the retry layer. The flow marks the request as this auth's to sign,
      and the client's transport stack calls :meth:`sign` on each attempt as it leaves the rate budget: after the last
      middleware, the cache and the rate-limit wait, and before the credential guard runs again. A retry, a hedge and
      a request that queued behind the budget each carry a signature of their own, so a short-lived one (a timestamp
      checked to 30 s, a JWT of two minutes) goes stale neither across the backoff nor in the queue, and a cross-origin
      redirect hop is signed only to have the guard withhold it. It signs only as
      the client's own ``auth`` under a headgate client: under a bare ``httpx2.AsyncClient``, or wrapped in another
      ``httpx2.Auth``, its requests go out unsigned and a 401 is not re-sent.
    * **An exception from** :meth:`sign` **surfaces from inside the retry layer**, as it was raised: the attempt counts
      (observers see an ``AttemptFinished`` with its type as the error), the request is not sent, and it is not
      retried. An ``httpx2.HTTPError`` or ``OSError`` is judged as a failed send instead.
    * **The body is read first**, so a signature may cover it. httpx2 honors ``requires_request_body`` only in its
      default flows, which this replaces.
    * **A request the caller signed is left alone**: :meth:`skips` returns ``True`` for one that already carries any
      of the signed headers or parameters, as an explicit header wins a clash with every auth here. Override it to
      leave other requests unsigned too, such as a provider's public reads. It is decided once per call, in the flow,
      so no attempt of a skipped request is signed.
    * **One re-send on a 401**, never a loop, signed afresh like every attempt; a second 401 reaches the caller as
      ``AuthError``. A 401 to a request this flow did not sign, or one sent without its signature (withheld from an
      origin the client does not trust), is not re-sent.

    **What it stamps is declared**, in :attr:`credential_headers` and :attr:`credential_params`, the names every
    :class:`Auth` and middleware declares with: what :meth:`sign` sets. The client withholds exactly those from an
    untrusted origin and the cache keeps them out of storage. A subclass that declares neither fails when it is defined.
    The parameters are registered as secret, so error messages redact them.

    **Its identity is the long-lived key.** :meth:`credential_identity` names the credential the signatures stand for,
    so a cache keeps one key's responses apart from another's. By default each instance is its own identity (random
    bytes drawn once); a subclass whose instances should share a cache partition when they hold the same key returns
    the key's secret material.
    """

    #: The headers :meth:`sign` sets (compared case-insensitively).
    credential_headers: ClassVar[frozenset[str]] = frozenset[str]()
    #: The query parameters :meth:`sign` sets.
    credential_params: ClassVar[frozenset[str]] = frozenset[str]()

    _identity: bytes | None = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a concrete subclass that declares nothing it signs with, and register its parameters as secret."""
        super().__init_subclass__(**kwargs)
        _refuse_final_overrides(cls, SigningAuth, "sign and skips")
        _refuse_async_hooks(cls, SigningAuth, "sign", "skips")
        declared = _declares_names(cls)
        if not inspect.isabstract(cls) and not declared:
            msg = (
                f"{cls.__name__} must declare the headers or parameters sign() sets, in credential_headers or "
                "credential_params"
            )
            raise TypeError(msg)

    @override
    def credential_identity(self) -> bytes:
        """Return bytes naming the key this auth signs with: by default, this instance's own, drawn once."""
        if self._identity is None:
            self._identity = secrets.token_bytes(32)
        return self._identity

    @abstractmethod
    def sign(self, request: httpx2.Request, now: float) -> None:
        """Sign *request* as it stands, at *now*, replacing any signature an earlier attempt set.

        *now* is the attempt's wall-clock time in Unix seconds, read from the client's timing (its ``wall_clock``), so
        a signer whose client is built under ``TestClock.install()`` signs on the test clock: sign with it rather than
        ``time.time()``. It
        runs once per attempt, so it may run several times on one request. The body is read (``request.content``). Set
        only the headers and parameters this class declares.
        """

    def skips(self, request: httpx2.Request) -> bool:
        """Return whether *request* is sent unsigned: by default, one already carrying a signed header or parameter."""
        return _carries(self, request)

    @final
    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        if self.skips(request):
            yield request
            return
        # The retry layer and the 401 path re-send this request, so a streamed body is buffered before the first send.
        await request.aread()
        # The transport stack signs each attempt (see SIGNER_EXT); the flow only says who signs.
        request.extensions[SIGNER_EXT] = self
        response = yield request
        # A 401 to a request its signature never reached (the credential guard withheld it) judged no signature, so it
        # is not re-sent.
        if response.status_code == httpx2.codes.UNAUTHORIZED and _carries(self, response.request):
            await _drop_body(response)
            yield request


@final
@sealed("yield Minted(token, lifetime) from mint, and read its token and lifetime")
@dataclass(frozen=True, slots=True)
class Minted[TokenT]:
    """The last step of a mint flow: the credential, and how long the provider says it lives (seconds)."""

    token: TokenT
    lifetime: float

    def __post_init__(self) -> None:
        """Refuse a lifetime that is not a number of seconds above 0; ``math.inf`` is a token that never expires.

        :raises TypeError: for a lifetime that is not a number, a ``bool`` included.
        :raises ValueError: for a number not above 0, NaN included.
        """
        wanted = "the seconds the token lives, a number above 0"
        check_number("Minted(lifetime=)", self.lifetime, wanted)
        if not self.lifetime > 0:
            msg = f"Minted(lifetime=) takes {wanted} (got {self.lifetime!r})"
            raise ValueError(msg)


#: What a token type may be read as that does not say it is not text: ``Any``, and a union, which may hold ``str``.
_MAYBE_TEXT: Final = (Any, UnionType)


def _token_type(cls: type) -> object | None:
    """Return the token type *cls* binds in its own ``CachedTokenAuth[...]`` base, when it is a class other than text.

    ``None`` when *cls* binds none there, binds ``str``, or binds what may be text or is no class here (``Any``, a
    union such as ``str | None``, a type variable a subclass binds later, a ``Literal``): the default
    :meth:`~CachedTokenAuth.attach` checks the token itself then. ``Any`` and a union are classes on 3.14, so they are
    named here.
    """
    for base in cls.__dict__.get("__orig_bases__", ()):
        if get_origin(base) is CachedTokenAuth:
            (token_type,) = get_args(base)
            kind = get_origin(token_type) or token_type
            if isinstance(kind, type) and kind not in _MAYBE_TEXT and not issubclass(kind, str):
                return token_type
    return None


#: Guards every token state's table of locks: clients on several threads, each with its event loop, may share one auth.
_LOCK_TABLE_GUARD: Final = threading.Lock()


@dataclass(slots=True)
class _Flight:
    """One event loop's mints of a :class:`CachedTokenAuth`: its lock, the mints started so far, the last that failed.

    An ``asyncio.Lock`` serves the one loop it first waits on, so the calls of each loop mint one at a time behind a
    lock of their own, and count their own mints: a call that queued behind a failed mint raises that mint's error, and
    a mint on another loop (one per thread) is no mint this call waited for.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attempts: int = 0
    minting: bool = False
    failed: _Failed | None = None


@dataclass(slots=True, kw_only=True)
class _TokenState[T]:
    """A :class:`CachedTokenAuth`'s cache: the clock its lifetimes run on, the token held, and each loop's mints.

    The token is shared by every client of the auth, on whatever loop it runs; the mints are each loop's
    (:meth:`flight`). ``identity`` is the instance's own credential identity, drawn once.
    """

    clock: Clock
    #: The ``id()`` of the auth this state belongs to: a copy of the auth shares the attribute, not the state.
    owner: int
    identity: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    held: _Held[T] | None = None
    #: The mints of each event loop the auth's calls run on (see :meth:`flight`), dropped with its loop.
    flights: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Flight] = field(
        default_factory=weakref.WeakKeyDictionary
    )
    #: Whether a client fixed :attr:`clock` (:func:`hold_timing`), rather than a first call or seed.
    held_by_client: bool = False

    def flight(self) -> _Flight:
        """Return the running event loop's mints, made on the loop's first call."""
        loop = asyncio.get_running_loop()
        with _LOCK_TABLE_GUARD:
            flight = self.flights.get(loop)
            if flight is None:
                flight = self.flights[loop] = _Flight()
        return flight

    def minted(self) -> bool:
        """Return whether a mint has started on any loop: the clock the token's lifetime runs on is then fixed."""
        with _LOCK_TABLE_GUARD:
            return any(flight.attempts for flight in self.flights.values())


#: The instance attribute a :class:`CachedTokenAuth`'s :class:`_TokenState` is kept under.
_STATE: Final = "_headgate_token_state"


def _state_of[T](auth: CachedTokenAuth[T], timing: Timing | None = None) -> _TokenState[T]:
    """Return *auth*'s state, made now if it has none, on *timing*'s clock or the timing current now.

    The base class has no constructor for a subclass to forget, so the state is made on first use: when a client is
    built with the auth (:func:`hold_timing`), or at its first call or :meth:`~CachedTokenAuth.seed` if that comes
    first. A copy of the auth (``copy.copy``) starts with a state of its own: two copies sharing one cache, lock and
    identity would be one auth under two names.
    """
    state = _existing_state(auth)
    if state is not None:
        return state
    # Under the guard, so two threads that find no state make one between them, not one each.
    with _LOCK_TABLE_GUARD:
        return _existing_state(auth) or _new_state(auth, timing)


def _new_state[T](auth: CachedTokenAuth[T], timing: Timing | None) -> _TokenState[T]:
    """Give *auth* a state of its own, on *timing*'s clock or the timing current now, and return it."""
    clock = (current_timing() if timing is None else timing).clock
    state = vars(auth)[_STATE] = _TokenState[T](clock=clock, owner=id(auth))
    return state


def _existing_state[T](auth: CachedTokenAuth[T]) -> _TokenState[T] | None:
    """Return *auth*'s own state, or ``None`` when it has none yet, or holds only the state of the auth it copies."""
    state = vars(auth).get(_STATE)
    return cast("_TokenState[T]", state) if isinstance(state, _TokenState) and state.owner == id(auth) else None


def check_timing(auth: Auth | None, timing: Timing, shared: Iterable[tuple[str, Clock]] = ()) -> None:
    """Refuse a :class:`CachedTokenAuth`, or a *shared* object, that already runs on another clock than *timing*'s.

    ``Client()`` calls it before it builds anything, so a refused client leaves no ``httpx2.AsyncClient`` behind, and
    :func:`hold_timing` last, once nothing else can fail. An auth with no state yet, or whose state holds no token,
    started no mint and was given to no client, may move to *timing*. A *shared* object, named by what it was given as,
    with the clock it was built on, cannot move: a shared rate budget, breaker registry or retry budget measures
    one quota, cooldown or retry window for every client, so its clients run on the timing it was built on.

    :raises TypeError: when the auth already runs on another clock: a client was built with it on another timing, or
        it holds a token or minted on another, whose lifetime that clock measures; and when a shared object does.
    """
    for what, clock in shared:
        if clock != timing.clock:
            msg = (
                f"the {what} runs on another timing than this client is built on, so the two would measure one quota "
                "or cooldown on two clocks: build it inside the same timing_override() or TestClock.install() block "
                "as its clients"
            )
            raise TypeError(msg)
    state = _existing_state(auth) if isinstance(auth, CachedTokenAuth) else None
    if state is None or state.clock == timing.clock:
        return
    if state.held_by_client or state.held is not None or state.minted():
        msg = (
            f"{type(auth).__name__} already runs its token lifetimes on another timing, which a client built with "
            "it or the token it holds was measured on: build all of its clients under one timing (one "
            "TestClock.install() block), and seed it inside the same block"
        )
        raise TypeError(msg)


def hold_timing(auth: Auth | None, timing: Timing) -> None:
    """Have a :class:`CachedTokenAuth` given to a client run its token lifetimes on the client's *timing*.

    A client is built inside ``TestClock.install()`` and may be called after the block, so its auth's clock is fixed
    when the client is built. :func:`check_timing` refused, before the client was built, an auth this would move off
    another clock it already runs on.
    """
    if isinstance(auth, CachedTokenAuth):
        state = _state_of(auth, timing)
        state.clock = timing.clock
        state.held_by_client = True


def _check_margin(cls: type[CachedTokenAuth[Any]]) -> None:
    """Refuse a ``refresh_margin`` that is not a finite number of seconds, 0 or more, naming *cls*.

    :raises TypeError: for a margin that is not a number, a ``bool`` included.
    :raises ValueError: for a negative, NaN or infinite one.
    """
    option = f"{cls.__name__}.refresh_margin"
    wanted = "the seconds before expiry to refresh, a finite number of 0 or more"
    check_number(option, cls.refresh_margin, wanted)
    if not 0 <= cls.refresh_margin < math.inf:
        msg = f"{option} takes {wanted} (got {cls.refresh_margin!r})"
        raise ValueError(msg)


class CachedTokenAuth[TokenT = str](Auth):
    """Bearer auth for providers that mint a short-lived token over the network.

    The token comes from an endpoint, so it is cached, refreshed before it expires, and refreshed again if it is
    rejected early. A subclass gets three behaviors:

    * **A lock-guarded mint.** httpx2 runs requests concurrently, so an unguarded refresh would let N in-flight
      requests each fire their own token exchange. The lock is the running event loop's own, so one auth may serve
      clients on several loops (one per thread): each loop's requests wait for one mint, and the loops mint apart.
    * **An expiry margin.** The token is re-minted ``refresh_margin`` before it expires (at half-life when the
      lifetime is shorter than twice the margin), because a token that expires mid-flight fails a request that may be
      a write that cannot be repeated.
    * **One forced re-mint on a 401**, never a loop, for a token revoked server-side before its stated lifetime, and
      none for a token minted in the last 60 seconds. A 401 on a token that new is a permissions
      problem, not staleness, and re-minting cannot fix it: a gateway that rejects valid tokens (a revoked scope, a
      changed entitlement) costs at most one mint per window, and the 401 reaches the caller as the ``AuthError`` it
      is. A 401 to a request sent without the token (withheld from an origin the client does not trust) rejects
      nothing, so it neither mints nor re-sends. The re-sent 401's body is dropped unread. Under a headgate client the
      call's body is not buffered for the re-send: a body that cannot be sent twice (a one-shot stream) is not re-sent,
      its 401 reaches the caller, and the rejected token is dropped so the next call mints.

    The exchange itself is the subclass's, because that is where providers differ (form-encoded or JSON, a separate
    sign-in host, their own field names). It supplies :meth:`mint`, an async generator that yields the requests the
    exchange needs, so they travel through the owning client's own stack (its transport, rate budget, retries and error
    mapping, and a test's ``fake_network``), each marked as an auth hop (its ``headgate.auth_hop`` request extension
    is ``True``, which a fixture recorder reads to leave it out), and ends by yielding :class:`Minted`. A mint that
    makes no request of its own (a vendor SDK's call) yields only the :class:`Minted`. A subclass without one cannot
    be built.

    Two more hooks let a
    credential that is not an ``Authorization`` bearer ride the same cache: :meth:`attach` (how a token is put on a
    request) and :meth:`skips` (a request that needs none). ``TokenT`` is the credential's type, ``str`` for every
    bearer; a subclass whose token is of another type overrides :meth:`attach`, or its definition fails (or, when the
    type is bound further down, as ``class Mid[T](CachedTokenAuth[T])``, its first call). :meth:`seed` caches a token
    obtained elsewhere.

    **It has no constructor to call.** The cache and its locks are made on first use, so a subclass's ``__init__`` sets
    only its own state. A token's lifetime runs on the timing of the clients built with the auth
    (:func:`headgate.timing.current_timing` when each is built), so one given to a client built inside
    ``TestClock.install()`` expires on the test clock. Its clients share that one timing: a client built on another,
    or on another than a token was seeded or minted on, raises ``TypeError`` before it builds anything. A copy
    (``copy.copy``, ``copy.deepcopy``) starts with an empty cache of its own, and a pickle holds no token.

    **What it stamps is declared**, in :attr:`credential_headers` and :attr:`credential_params`, the names every
    :class:`Auth` and middleware declares with: the headers :meth:`attach` sets (``authorization`` by default) and the
    query parameters (none by default, ``access_token`` for a provider that takes the token in the query). The client
    withholds exactly those from an untrusted origin, and the parameters are registered as secret, so error messages
    redact them. A subclass that overrides :meth:`attach` must declare them again, on that class or one below it, not
    both empty, or the class definition fails: an undeclared credential would follow a request anywhere.

    **Its identity is the long-lived credential, not the token.** :meth:`credential_identity` names what the minted
    tokens stand for, so a cache keeps one credential's responses apart from another's across token refreshes. The base
    class cannot see the credential a subclass mints with, so by default each instance is its own identity (random
    bytes drawn once): never shared, which is safe, but two instances holding the same credential share nothing either.
    A subclass that wants them to overrides :meth:`credential_identity` to return its long-lived secret material (the
    API key, or client id and secret), never the short-lived token.

    The hops a :meth:`mint` yields are addressed by the subclass's own code, so each may carry credentials to the
    origin it names, and the redirects it is answered with stay pinned to that origin: one to another origin is refused
    as ``ForbiddenDestinationError`` rather than followed, since the hop's own headers and body may carry the long-lived
    credential (see :mod:`headgate.security`).

    **Every hop counts toward** ``HttpOptions.max_redirects``. httpx2 keeps one history per logical request, and puts
    each response an auth flow answers with in it beside the redirects, so a mint of *n* hops (and the response a 401
    re-mint answers) uses *n* of the call's redirect allowance. A client whose mint makes several hops, or whose
    endpoints redirect too, raises ``max_redirects`` to cover both; past it the call raises ``TransportError``
    ("Exceeded maximum allowed redirects").

    The lock is held while a mint flow's requests are in flight, so requests on its loop that need a token wait for the
    mint to finish or fail, bounded by the hop's timeouts (the call's, unless the hop sets its own) and the call's
    deadline.
    Closing the flow (an error, or the caller's cancellation) releases it. **A mint that fails fails every request that
    waited for it**, each with a copy of its ``ClientError``, instead of each minting again in turn; a request that
    starts after the failure mints afresh. **A refresh that fails inside the margin** sends the held token instead,
    since it has not expired, and so do the requests that waited for that refresh. A hop's network failure is raised
    by httpx2 outside the flow, so it is neither shared nor answered with the held token.

    **A failed mint is reported as the token endpoint's failure**, never the API call's: an exception the mint code
    raises (a ``ConnectError`` from a vendor SDK,
    a ``raise_for_status()``, an unreadable token response) becomes the ``ClientError`` it means, named for this auth
    class and the token endpoint, chained to the original. It is raised from the auth flow, so the API host's circuit
    breaker records nothing for it. A hop :meth:`mint` yields travels the owning client's stack; a network
    failure there is reported the same way, as ``TransportError`` naming this auth class and the hop's endpoint, and
    reading its response (bounded at 1 MiB) fails
    through the same mapping, so a body that is too large is ``ResponseTooLargeError`` and a dropped connection
    ``TransportError``.
    """

    #: The headers :meth:`attach` sets (compared case-insensitively). A subclass whose :meth:`attach` sets others
    #: declares them here.
    credential_headers: ClassVar[frozenset[str]] = frozenset({"authorization"})
    #: The query parameters :meth:`attach` sets. A subclass whose :meth:`attach` puts the token in the query declares
    #: them here.
    credential_params: ClassVar[frozenset[str]] = frozenset[str]()

    #: Seconds before a token expires that it is re-minted, at half its life when that is shorter. A subclass sets its
    #: own as a class attribute, checked when the class is defined (:func:`_check_margin`).
    refresh_margin: ClassVar[float] = 120.0

    # -- Subclass contract ---------------------------------------------------

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a subclass with no mint, or whose :meth:`attach` sets a credential it has not declared.

        When a subclass overrides :meth:`attach`, that class or one below it must declare :attr:`credential_headers` or
        :attr:`credential_params`, not both empty; when it keeps the default, :attr:`credential_headers` must name
        ``authorization``. Its parameters are registered as secret, and its
        :attr:`refresh_margin` is checked.
        """
        super().__init_subclass__(**kwargs)
        _refuse_final_overrides(cls, CachedTokenAuth, "mint, attach and skips")
        _refuse_async_hooks(cls, CachedTokenAuth, "attach", "skips")
        _check_mint(cls)
        declared = _declares_names(cls)
        _check_margin(cls)
        mro = cls.__mro__
        attaching = next(klass for klass in mro if "attach" in vars(klass))
        # The default attach sets Authorization, and a 401 re-mints only for a request that carried a declared name.
        if attaching is CachedTokenAuth and "authorization" not in {name.lower() for name in cls.credential_headers}:
            msg = (
                f"{cls.__name__} keeps the default attach, which sets Authorization, so credential_headers must name "
                f"authorization (got {sorted(cls.credential_headers)}): add it, or override attach to put the token "
                "where the declaration says"
            )
            raise TypeError(msg)
        below = [vars(klass) for klass in mro[: mro.index(attaching) + 1]]
        redeclared = any(name in names for names in below for name in _DECLARATIONS)
        if not (redeclared and declared):
            msg = (
                f"{cls.__name__} overrides attach, so it must declare what attach sets in credential_headers or "
                "credential_params"
            )
            raise TypeError(msg)
        token_type = _token_type(cls)
        if token_type is not None and attaching is CachedTokenAuth:
            msg = (
                f"{cls.__name__} holds a {type_text(token_type)} token, so it must override attach: the default sends "
                "a str as Authorization: Bearer"
            )
            raise TypeError(msg)

    # -- What it stamps -------------------------------------------------------

    @override
    def credential_identity(self) -> bytes:
        """Return bytes naming the long-lived credential this auth mints with: by default, this instance's own.

        A subclass whose instances should share a cache partition when they hold the same credential returns that
        credential's secret material here, never the short-lived token, which changes on every refresh.
        """
        return self.__state.identity

    @property
    def __state(self) -> _TokenState[TokenT]:
        """The token cache and its lock, made on first use (see :func:`_state_of`)."""
        return _state_of(self)

    @override
    def __getstate__(self) -> dict[str, object]:
        """Return what a copy or a pickle takes: the instance's attributes, but not its token state.

        A copy starts with an empty cache of its own anyway; the state holds each event loop's mints, which no pickle
        can hold, and the token, which no pickle should.
        """
        # shortcut: a subclass's __slots__ values are left out too; read object.__getstate__'s pair once one has them.
        return {name: value for name, value in vars(self).items() if name != _STATE}

    @abstractmethod
    def mint(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request | Minted[TokenT], httpx2.Response]:
        """Mint a token: yield each request the exchange needs, and end by yielding :class:`Minted`.

        Write it as an ``async def`` that yields. Each request travels the owning client's stack, and its response is
        sent back in already read (up to 1 MiB), so ``.content`` and ``.json()`` work on it. A hop that sets no
        ``timeout`` extension is sent with the call's timeouts, and every hop's timeouts are cut to what the call's
        deadline leaves. A mint with no request of its own yields only the :class:`Minted`. *request* is the call that
        needs the credential, for a hop that wants another of its extensions. Called with its event loop's lock held,
        so an implementation need not guard against concurrent mints on one loop; an auth shared by clients on several
        loops (threads) may mint on two at once. Raise rather than yield an empty token. A mint whose
        provider may rotate its long-lived credential calls :meth:`reject_credential_rotation` with the returned value.
        """

    def attach(self, request: httpx2.Request, token: TokenT) -> None:
        """Put *token* on *request*, replacing any earlier one: the re-send after a 401 calls this a second time.

        An override declares the headers it sets in :attr:`credential_headers`. The default sends a ``str`` token as
        ``Authorization: Bearer <token>``, and raises ``TypeError`` for any other, before setting anything: a token of
        another type is attached by an override.
        """
        if not isinstance(token, str):
            msg = (
                f"{type(self).__name__} holds a {type(token).__name__} token, which the default attach cannot send "
                "as Authorization: Bearer: override attach to put it on the request"
            )
            raise TypeError(msg)
        request.headers["Authorization"] = f"Bearer {token}"

    def skips(self, request: httpx2.Request) -> bool:
        """Return whether *request* needs no credential from this auth: by default, one carrying its token's name."""
        return _carries(self, request)

    @final
    def reject_credential_rotation(self, returned: str | None, configured: str, *, setting: str) -> None:
        """Raise ``ClientConfigurationError`` when a token endpoint returns a replacement for the configured credential.

        When a token endpoint returns a replacement for the credential used to call it, the configured one is dead and
        the replacement exists only in this response. An in-memory cache cannot persist it, so the only safe answer is
        to fail on the first exchange and say why: discarding the replacement silently buys one working call, then an
        opaque ``400`` once the access token expires. An absent or empty *returned*, or one equal to *configured*, is
        benign and common. *setting* names where the operator configured the credential, for the error message, which
        is named for this auth class, as every mint failure is.

        :raises TypeError: for a *returned* that is neither a ``str`` nor ``None``, or a *configured* that is not a
            ``str``, naming both kinds: ``b"key"`` never equals ``"key"``, so it would read as a rotated credential.
        """
        provider = type(self).__name__
        if not (isinstance(configured, str) and isinstance(returned, str | None)):
            msg = (
                f"returned= and configured= take the two credentials as str, to compare them (got {named(returned)} "
                f"against {named(configured)}): pass each as text, decoding one read as bytes"
            )
            raise TypeError(msg)
        if returned is None or returned in {"", configured}:
            return
        msg = (
            f"{provider}'s token endpoint returned a new long-lived credential, which means this key is configured for "
            f"rotating credentials. This client cannot use that: the replacement would have to be persisted durably on "
            f"every exchange, and the configured {setting} is now dead. Re-authorize with a non-rotating key and set "
            f"{setting} to the new value."
        )
        raise ClientConfigurationError(msg, source=provider)

    # -- Cache ---------------------------------------------------------------

    def seed(self, minted: Minted[TokenT]) -> None:
        """Cache *minted*, a token obtained outside this auth with its lifetime left to live, in seconds.

        For a token restored from storage, or a test's fixed bearer. Its age is unknown, so it never counts as fresh:
        a 401 on it re-mints.

        :raises TypeError: for a *minted* that is not a :class:`Minted`, such as the token alone.
        """
        check_kind("minted", minted, Minted, "a Minted(token, lifetime)")
        self._cache(minted.token, minted.lifetime, minted_at=-math.inf)

    def _cache(self, token: TokenT, lifetime: float, *, minted_at: float) -> None:
        # A lifetime at or under the margin would read as due the moment it was cached, so every request would mint
        # again: refresh at half-life instead.
        # The class's margin, which was checked: an instance's own assignment is not read.
        margin = min(type(self).refresh_margin, lifetime / 2)
        now = self.__state.clock()
        self.__state.held = _Held(
            token=token, minted_at=minted_at, due_at=now + lifetime - margin, expires_at=now + lifetime
        )

    def _usable(self, rejected: TokenT | None) -> TokenT | None:
        """Return the cached token unless it is absent, the one a 401 just rejected, or within the refresh margin."""
        held = self.__state.held
        if held is not None and held.token != rejected and self.__state.clock() < held.due_at:
            return held.token
        return None

    def _unexpired(self, rejected: TokenT | None) -> TokenT | None:
        """Return the cached token while it has not expired, unless it is the one a 401 just rejected.

        A refresh inside the margin that fails falls back to it: it still carries the authority it was minted with.
        """
        held = self.__state.held
        if held is not None and held.token != rejected and self.__state.clock() < held.expires_at:
            return held.token
        return None

    def _forget(self, token: TokenT) -> None:
        """Drop *token* from the cache if it is still the cached one, so the next call mints."""
        if self.__state.held is not None and self.__state.held.token == token:
            self.__state.held = None

    def _is_fresh(self, token: TokenT) -> bool:
        """Return whether *token* is still the cached one and was minted within :data:`FRESH_TOKEN_S`.

        A rejected token that is no longer cached was already replaced by another request, so the replacement is worth
        a retry, and the cache hands it over without minting.
        """
        held = self.__state.held
        return held is not None and token == held.token and self.__state.clock() - held.minted_at < FRESH_TOKEN_S

    async def _mint_step[T](self, step: Awaitable[T]) -> T:
        """Await a *step* of the mint, raising a failure in it as the ``ClientError`` it means.

        The mapped error is raised outside the ``except`` block, with no cause: the original's message (httpx2's names
        the URL as sent) may carry the token URL's secrets, and a traceback prints the whole chain. A note names the
        original by its type and redacted message instead.
        """
        try:
            return await step
        except Exception as exc:
            mapped = mint_error(exc, source=type(self).__name__)
            if mapped is exc:
                raise
            detail = " ".join(redact_secrets(str(exc)).split())
            mapped.add_note(f"from {type(exc).__name__}: {shorten(detail)}" if detail else f"from {type(exc).__name__}")
        raise mapped

    @final
    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        if self.skips(request):
            yield request
            return

        context = CallContext.find(request)
        await _buffer_unless_headgate(request, context)
        # The token a 401 came back for, rather than a "force" flag: each waiter on the lock re-checks the cache
        # against the token it was rejected for, so concurrent 401s share the first replacement instead of each minting.
        rejected: TokenT | None = None
        while True:
            _mark_minting(context, minting=True)
            flight = self.__state.flight()
            # The mint this call waits behind: the one in flight on its loop, or else the next to start.
            behind = flight.attempts if flight.minting else flight.attempts + 1
            async with flight.lock:
                token = self._usable(rejected)
                if token is None:
                    token = self._after_failed_wait(flight, rejected, behind)
                if token is None:
                    flight.attempts += 1
                    flight.minting = True
                    try:
                        # Only this generator can yield to the client, so it drives the mint flow: httpx2 sends each
                        # request the mint yields and hands its response back in.
                        mint = self.mint(request)
                        try:
                            step = await self._mint_step(anext(mint))
                            while isinstance(step, httpx2.Request):
                                _prepare_hop(step, request, type(self).__name__)
                                hop_response = yield step
                                hop_response = await self._mint_step(_read_hop(hop_response, type(self).__name__))
                                step = await self._mint_step(mint.asend(hop_response))
                        except StopAsyncIteration:
                            msg = f"{type(self).__name__}.mint ended without yielding Minted"
                            raise RuntimeError(msg) from None
                        finally:
                            await mint.aclose()
                    except ClientError as exc:
                        token = self._after_failed_mint(flight, exc, rejected)
                    else:
                        flight.failed = None
                        token = step.token
                        self._cache(token, step.lifetime, minted_at=self.__state.clock())
                    finally:
                        flight.minting = False
            _mark_minting(context, minting=False)
            before = stamped_names(request)
            self.attach(request, token)
            refuse_undeclared(self, "attach", request, before)
            response = yield request
            # ``rejected`` is set only after the first 401, so this re-sends at most once. A 401 to a request the token
            # never reached (the credential guard withheld it from an untrusted origin, or httpx2 from a cross-origin
            # redirect) judged no token, so it rejects none.
            if not await self._re_sends(response, token, rejected, context):
                return
            rejected = token

    def _after_failed_wait(self, flight: _Flight, rejected: TokenT | None, behind: int) -> TokenT | None:
        """Return the token a call that waited behind *flight*'s mint number *behind* uses, or ``None`` to mint itself.

        When that mint failed, its error is this call's answer too (raised as a copy), unless the held token has not
        expired yet: then that token is used, as it is by the call whose mint failed.
        """
        failed = flight.failed
        if failed is None or failed.attempt < behind:
            return None
        token = self._unexpired(rejected)
        if token is None:
            raise failed.raised()
        return token

    def _after_failed_mint(self, flight: _Flight, error: ClientError, rejected: TokenT | None) -> TokenT:
        """Record *error* for the calls waiting behind *flight*'s mint, and return the held token if it has not expired.

        :raises ClientError: *error*, when there is no such token.
        """
        flight.failed = _Failed(flight.attempts, _copied(error))
        token = self._unexpired(rejected)
        if token is None:
            raise error
        return token

    async def _re_sends(
        self, response: httpx2.Response, token: TokenT, rejected: TokenT | None, context: CallContext | None
    ) -> bool:
        """Return whether the flow re-sends its request after *response*, the answer to it carrying *token*.

        Only a first 401 that rejected a long-held token is re-sent (see the class docstring), and only with a body
        that can be sent again: a one-shot stream's 401 reaches the caller, and the token is dropped so the next call
        mints afresh. A re-sent 401's body is dropped unread.
        """
        if (
            response.status_code != httpx2.codes.UNAUTHORIZED
            or rejected is not None
            or not _carries(self, response.request)
            or self._is_fresh(token)
        ):
            return False
        if context is not None and context.get(REPLAYABLE_BODY) is False:
            self._forget(token)
            return False
        await _drop_body(response)
        return True
