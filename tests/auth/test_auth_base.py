# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``auth=`` takes a :class:`headgate.Auth`, which declares what it stamps, so no auth's credential leaves its origin.

A plain ``httpx2.Auth`` declares nothing, so the guard could not know which of its headers to withhold from another
origin: the client refuses it when it is built, ``httpx2.BasicAuth`` and ``httpx2.DigestAuth`` included.
"""

from __future__ import annotations

import annotationlib
import asyncio
import inspect
import sys
from typing import TYPE_CHECKING, Any, ClassVar, cast, override

import httpx2
import pytest

import headgate
from headgate import (
    BearerAuth,
    CachedTokenAuth,
    Client,
    HttpOptions,
    Minted,
    QueryParamAuth,
    SigningAuth,
    StaticHeaderAuth,
    as_json,
)
from headgate.security import CredentialStamper
from headgate.testing import FakeTransport, Respond

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


class _PlainKeyAuth(httpx2.Auth):
    """A plain httpx2 auth that stamps a key and declares nothing."""

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["X-Api-Key"] = "secret"
        yield request


class _KeyAuth(headgate.Auth):
    """A key auth that declares its header as a class attribute."""

    credential_headers: ClassVar = frozenset({"x-api-key"})
    credential_params: ClassVar = frozenset[str]()

    @override
    def credential_identity(self) -> bytes:
        return b"secret"

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["X-Api-Key"] = "secret"
        yield request


class _PropertyKeyAuth(headgate.Auth):
    """The same auth, declaring through properties, as headgate's own static auths do."""

    @property
    @override
    def credential_headers(self) -> frozenset[str]:
        return frozenset({"x-api-key"})

    @property
    @override
    def credential_params(self) -> frozenset[str]:
        return frozenset[str]()

    @override
    def credential_identity(self) -> bytes:
        return b"secret"

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["X-Api-Key"] = "secret"
        yield request


@pytest.mark.parametrize(
    "auth",
    [_PlainKeyAuth(), httpx2.BasicAuth("user", "pass"), httpx2.DigestAuth("user", "pass")],
    ids=["plain", "basic", "digest"],
)
def test_a_plain_httpx2_auth_is_refused(auth: httpx2.Auth) -> None:
    """Refuse an auth that declares nothing, naming it and what to write instead."""
    with pytest.raises(TypeError, match=r"auth= takes a headgate\.Auth") as raised:
        Client(base_url="https://api.example", auth=cast("Any", auth))
    assert type(auth).__qualname__ in str(raised.value)
    assert "StaticHeaderAuth" in str(raised.value)


@pytest.mark.parametrize("auth", [_KeyAuth(), _PropertyKeyAuth()], ids=["class-attribute", "property"])
async def test_a_declared_auth_is_withheld_cross_origin(auth: headgate.Auth) -> None:
    """Withhold a declared auth's own header from a redirect to another origin, and keep it on its own."""
    fake = (
        FakeTransport()
        .reply("GET", "/me", Respond(302, headers={"Location": "https://evil.example/steal"}))
        .reply("GET", "/steal", Respond(json={}))
    )
    async with Client(base_url="https://api.example", auth=auth, http=HttpOptions(transport=fake)) as client:
        await client.get("me", expect=as_json)

    assert [(call.url.host, call.headers.get("x-api-key")) for call in fake.calls] == [
        ("api.example", "secret"),
        ("evil.example", None),
    ]


def test_an_auth_missing_a_member_cannot_be_built() -> None:
    """Refuse to build an auth that leaves out whose credential it carries."""

    class Forgetful(headgate.Auth):
        credential_headers: ClassVar = frozenset({"x-api-key"})
        credential_params: ClassVar = frozenset[str]()

    with pytest.raises(TypeError, match="credential_identity"):
        cast("Any", Forgetful)()


@pytest.mark.parametrize("auth_class", [StaticHeaderAuth, BearerAuth, QueryParamAuth, SigningAuth, CachedTokenAuth])
def test_every_headgate_auth_is_an_auth(auth_class: type) -> None:
    """Derive every auth headgate ships from the one base an auth is."""
    assert issubclass(auth_class, headgate.Auth)


def test_a_virtual_subclass_is_refused() -> None:
    """Refuse an auth only registered as an ``Auth``: registering declares nothing it stamps."""

    class Registered(httpx2.Auth):
        """A plain auth made to pass ``isinstance(..., Auth)``."""

    headgate.Auth.register(Registered)

    with pytest.raises(TypeError, match=r"auth= takes a headgate\.Auth, .*Registered\)"):
        Client(base_url="https://api.example", auth=cast("Any", Registered()))


def test_an_auth_is_a_credential_stamper_by_its_shape() -> None:
    """Keep ``Auth`` a ``CredentialStamper`` structurally, not by a protocol base, so its constructor stays its own."""
    assert CredentialStamper not in headgate.Auth.__mro__
    assert isinstance(_Digest("user", "pass"), CredentialStamper)


async def test_an_auth_is_assignable_to_a_credential_stamper(tmp_path: Path) -> None:
    """Have ty accept an ``Auth`` where a ``CredentialStamper`` goes, so the base and the protocol stay in step."""
    module = tmp_path / "probe.py"
    module.write_text(
        "from headgate import Auth\n"
        "from headgate.security import CredentialStamper\n\n\n"
        "def stamper(auth: Auth) -> CredentialStamper:\n"
        "    return auth\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ty", "check", str(module), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0, stdout.decode() + stderr.decode()


def test_a_cooperative_mixin_constructor_still_runs() -> None:
    """Run a mixin's ``__init__`` listed after an auth base: ``Auth`` defines none to shadow it (R10)."""

    class _Mixin:
        def __init__(self) -> None:
            super().__init__()
            self.mixed = True

    class _Mixed(SigningAuth, _Mixin):
        credential_headers: ClassVar = frozenset({"X-Sig"})

        @override
        def sign(self, request: httpx2.Request, now: float) -> None:
            request.headers["X-Sig"] = "sig"

    assert _Mixed().mixed


class _Digest(headgate.Auth, httpx2.DigestAuth):
    """``httpx2.DigestAuth`` as an ``Auth``, as the auth guide writes it: its two declarations and its identity."""

    credential_headers: ClassVar = frozenset({"authorization"})
    credential_params: ClassVar = frozenset[str]()

    @override
    def credential_identity(self) -> bytes:
        return b"user:pass"


async def test_digest_auth_is_three_lines_of_auth() -> None:
    """Answer a digest challenge through ``_Digest``, httpx2's own flow, and withhold its header cross-origin."""
    seen: list[tuple[str, str | None]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host != "api.example":
            return httpx2.Response(200, json={})
        if "authorization" not in request.headers:
            return httpx2.Response(401, headers={"WWW-Authenticate": 'Digest realm="r", nonce="n", qop="auth"'})
        return httpx2.Response(302, headers={"Location": "https://cdn.example/blob"})

    auth = _Digest("user", "pass")
    async with Client(
        base_url="https://api.example", auth=auth, http=HttpOptions(transport=httpx2.MockTransport(handle))
    ) as c:
        await c.get("x", expect=as_json)

    (first, challenged, hop) = seen
    assert first == ("api.example", None)
    assert challenged[1] is not None
    assert challenged[1].startswith("Digest ")
    assert hop == ("cdn.example", None)


@pytest.mark.parametrize(
    ("auth_class", "signature"),
    [
        (headgate.Auth, "()"),
        (SigningAuth, "()"),
        (CachedTokenAuth, "()"),
        (StaticHeaderAuth, "(headers: 'Mapping[str, str]') -> 'None'"),
        (BearerAuth, "(token: 'str') -> 'None'"),
        (QueryParamAuth, "(params: 'Mapping[str, str]') -> 'None'"),
    ],
    ids=lambda value: getattr(value, "__name__", ""),
)
def test_each_auth_reads_as_what_its_constructor_takes(auth_class: type, signature: str) -> None:
    """Show each constructor's own parameters, not the ``(*args, **kwargs)`` the protocol base put there."""
    assert str(inspect.signature(auth_class, annotation_format=annotationlib.Format.STRING)) == signature


def test_an_auth_whose_class_escaped_the_checks_is_refused() -> None:
    """Refuse at ``Client()`` a leaf below a base whose ``__init_subclass__`` skips ``super()`` (R4)."""

    class _Quiet(SigningAuth):
        def __init_subclass__(cls, **kwargs: object) -> None:
            del cls, kwargs  # skips super().__init_subclass__(), so no check runs for a subclass

    class _Leaf(_Quiet):
        @override
        def sign(self, request: httpx2.Request, now: float) -> None:
            request.headers["X-Key"] = "secret"  # declared nowhere

    with pytest.raises(TypeError, match=r"_Leaf was not checked when it was defined: .*super\(\)\.__init_subclass__"):
        Client(base_url="https://api.example", auth=_Leaf())


def test_every_auth_headgate_ships_was_checked() -> None:
    """Accept the shipped auths and a well-behaved subclass, each checked when its class was defined."""
    for auth in (
        BearerAuth("t"),
        StaticHeaderAuth({"X-Api-Key": "k"}),
        QueryParamAuth({"api_key": "v"}),
        _Digest("u", "p"),
    ):
        Client(base_url="https://api.example", auth=auth)


class _Signer(SigningAuth):
    """A signer that sends its key id beside the signature, as an exchange's auth does."""

    credential_headers: ClassVar = frozenset({"X-Api-Key", "X-Signature"})

    @override
    def sign(self, request: httpx2.Request, now: float) -> None:
        request.headers["X-Api-Key"] = "key-1"
        request.headers["X-Signature"] = f"signed-at-{now}"


class _Tokens(CachedTokenAuth):
    """A token auth whose mint makes no request of its own."""

    @override
    async def mint(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request | Minted[str], httpx2.Response]:
        yield Minted("t0k3n", 3600.0)


#: Each kind of auth, with a header it sets, spelled in another case than it declares it.
_DEFAULT_CLASHES = [
    (_Signer(), "x-api-key"),
    (_Tokens(), "Authorization"),
    (BearerAuth("t0k3n"), "AUTHORIZATION"),
    (StaticHeaderAuth({"X-Api-Key": "k"}), "X-API-KEY"),
    (_KeyAuth(), "X-Api-Key"),
]
_DEFAULT_CLASH_IDS = ["signing", "cached-token", "bearer", "static", "own"]


@pytest.mark.parametrize(("auth", "header"), _DEFAULT_CLASHES, ids=_DEFAULT_CLASH_IDS)
def test_a_client_wide_header_the_auth_sets_is_refused(auth: headgate.Auth, header: str) -> None:
    """Refuse a default header the auth declares: every request would carry it, so the auth would never send its own."""
    with pytest.raises(ValueError, match=rf"^HttpOptions\(headers=\) names {header}, which {type(auth).__name__} sets"):
        Client(base_url="https://api.example", auth=auth, http=HttpOptions(headers={header: "caller", "X-Tenant": "7"}))


@pytest.mark.parametrize(("auth", "header"), _DEFAULT_CLASHES, ids=_DEFAULT_CLASH_IDS)
def test_a_views_header_the_auth_sets_is_refused(auth: headgate.Auth, header: str) -> None:
    """Refuse a view's header the auth declares, where the view is made, for the same reason."""
    client = Client(base_url="https://api.example", auth=auth)

    refusal = rf"^with_options\(headers=\) names {header}, which {type(auth).__name__} sets"
    with pytest.raises(ValueError, match=refusal):
        client.with_options(headers={header: "caller"})
    client.with_options(headers={"X-Tenant": "7"})


async def test_a_calls_own_header_is_still_the_callers_credential() -> None:
    """Send a call's own declared header as the caller's credential, unsigned, as a call that signs itself expects."""
    fake = FakeTransport().reply("GET", "/x", Respond(json={}))
    async with Client(base_url="https://api.example", auth=_Signer(), http=HttpOptions(transport=fake)) as client:
        await client.get("x", expect=as_json, headers={"X-Signature": "the-callers"})
        await client.get("x", expect=as_json)

    callers, signed = (call.headers["x-signature"] for call in fake.calls)
    assert callers == "the-callers"
    assert signed.startswith("signed-at-")
