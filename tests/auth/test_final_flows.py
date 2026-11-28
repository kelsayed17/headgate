# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The flow of ``SigningAuth`` and ``CachedTokenAuth`` is not a hook: it holds their guarantees, so it is final.

Their hooks are ``sign``, ``skips``, ``attach`` and ``mint``; a per-request header belongs in a middleware. An override
of the flow passed ty and failed deep inside the call (E13), so the type checker reports it and the class statement
refuses it.
"""

from __future__ import annotations

import asyncio
import gc
import sys
from textwrap import dedent
from typing import TYPE_CHECKING, Any, cast

import pytest

from headgate import Auth, BearerAuth, CachedTokenAuth, QueryParamAuth, SigningAuth, StaticHeaderAuth
from headgate._auth import derives_from

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    import httpx2


def _flow(_self: object, request: object) -> object:
    """Stand in for an override; never called."""
    return request


#: A declaration, so the class statement reaches the check of its final members.
_DECLARED = {"credential_headers": frozenset({"x-sig"})}


async def _mint(_self: object, _request: object) -> AsyncGenerator[object, object]:
    """Stand in for a token mint; never called."""
    yield None


def _identity(_self: object) -> bytes:
    """Name an auth's credential; never called."""
    return b"tenant"


@pytest.mark.parametrize(
    ("define", "refusal"),
    [
        (
            lambda: type("_Tenant", (SigningAuth,), {"async_auth_flow": _flow, **_DECLARED}),
            "overrides async_auth_flow, which SigningAuth runs itself: its hooks are sign and skips",
        ),
        (
            lambda: type("_Tenant", (CachedTokenAuth,), {"async_auth_flow": _flow, **_DECLARED}),
            "overrides async_auth_flow, which CachedTokenAuth runs itself: its hooks are mint, attach and skips",
        ),
        (
            lambda: type("_Tenant", (CachedTokenAuth,), {"reject_credential_rotation": _flow, **_DECLARED}),
            "overrides reject_credential_rotation, which CachedTokenAuth runs itself",
        ),
    ],
    ids=["signing-flow", "token-flow", "rotation-check"],
)
def test_overriding_a_final_member_is_refused_when_defined(define: Callable[[], type], refusal: str) -> None:
    """Refuse a subclass that overrides the flow or the rotation check (E13), naming the member and the hooks."""
    with pytest.raises(TypeError, match=f"_Tenant {refusal}"):
        define()


@pytest.mark.parametrize(
    ("base", "flow"),
    [
        (StaticHeaderAuth, "StaticHeaderAuth"),
        (BearerAuth, "StaticHeaderAuth"),
        (QueryParamAuth, "QueryParamAuth"),
        (SigningAuth, "SigningAuth"),
        (CachedTokenAuth, "CachedTokenAuth"),
    ],
    ids=["static", "bearer", "query", "signing", "token"],
)
def test_an_auth_flow_override_that_never_runs_is_refused_when_defined(base: type, flow: str) -> None:
    """Refuse an ``auth_flow`` override below an ``async_auth_flow`` of headgate's, the only flow a client runs.

    httpx2's own ``async_auth_flow`` drives ``auth_flow``; one of headgate's auths never calls it, so the override,
    which a tenant header or a per-request signature was written in, would be accepted and never run.
    """
    message = (
        rf"^_Tenant overrides auth_flow, which {flow}'s async_auth_flow, the flow a client runs, never calls: a header "
        r"computed per request belongs in a SigningAuth's sign\(\)$"
    )
    with pytest.raises(TypeError, match=message):
        type("_Tenant", (cast("Any", base),), {"auth_flow": _flow, **_DECLARED})


def test_an_auth_flow_override_below_a_flow_of_your_own_is_accepted() -> None:
    """Accept an ``auth_flow`` override below an ``async_auth_flow`` you wrote, which may drive ``auth_flow`` itself."""

    async def drives_auth_flow(self: Auth, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        yield next(self.auth_flow(request))

    members = {"async_auth_flow": drives_auth_flow, "credential_identity": _identity, "credential_params": frozenset()}
    base = type("_Own", (Auth,), {**members, **_DECLARED})
    subclass = type("_Tenant", (base,), {"auth_flow": _flow})

    assert subclass.auth_flow is _flow


@pytest.mark.parametrize(
    "bases", [(CachedTokenAuth, SigningAuth), (SigningAuth, CachedTokenAuth)], ids=["token-first", "signing-first"]
)
def test_one_auth_cannot_be_both_a_token_and_a_signer(bases: tuple[type, ...]) -> None:
    """Refuse a class of both bases, each of which runs its own flow, saying so and what to write instead.

    It inherits the flow of whichever base comes first, which the other's check took for an override the class never
    wrote.
    """
    message = (
        r"^_Both subclasses CachedTokenAuth and SigningAuth, which cannot be combined in one auth: each runs a flow "
        r"of its own\. Subclass CachedTokenAuth for the token, and sign each attempt in a middleware, which sees the "
        r"token on the request and declares what it sets$"
    )
    with pytest.raises(TypeError, match=message):
        type("_Both", bases, {"credential_headers": frozenset({"authorization", "x-sig"})})


@pytest.mark.parametrize(
    "bases", [(CachedTokenAuth, SigningAuth), (SigningAuth, CachedTokenAuth)], ids=["token-first", "signing-first"]
)
def test_a_refused_class_of_both_bases_does_not_confuse_the_next(bases: tuple[type, ...]) -> None:
    """Tell a signer from a token auth by its bases while a refused class of both lingers, uncollected.

    The refused class stays among its bases' subclasses until it is collected, with no ABC data of its own, so an
    ``issubclass`` against either base reads the other's cache through it: a plain signer read as a token auth, and was
    refused as both.
    """
    gc.disable()
    try:
        with pytest.raises(TypeError, match="cannot be combined in one auth"):
            type("_Both", bases, {"credential_headers": frozenset({"authorization", "x-sig"})})
        signer = type("_Signer", (SigningAuth,), {"sign": _flow, "credential_identity": _identity, **_DECLARED})
        token = type("_Token", (CachedTokenAuth,), {"mint": _mint, "credential_identity": _identity})

        assert derives_from(signer, SigningAuth)
        assert not derives_from(signer, CachedTokenAuth)
        assert not derives_from(token, SigningAuth)
    finally:
        gc.enable()


async def test_the_type_checker_reports_an_overridden_flow(tmp_path: Path) -> None:
    """Report ``override-of-final-method`` for the reviewer's tenant-header signer (``ext_override_flow.py``)."""
    module = tmp_path / "probe.py"
    module.write_text(
        dedent(
            """
            from collections.abc import AsyncGenerator
            from typing import ClassVar

            import httpx2

            from headgate import SigningAuth


            class Signer(SigningAuth):
                credential_headers: ClassVar = frozenset({"X-Sig"})

                def sign(self, request: httpx2.Request, now: float) -> None:
                    request.headers["X-Sig"] = "sig"

                async def async_auth_flow(
                    self, request: httpx2.Request
                ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
                    request.headers["X-Tenant"] = "t1"
                    async for sent in super().async_auth_flow(request):
                        yield sent
            """
        )
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ty", "check", str(module), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    assert "override-of-final-method" in stdout.decode() + stderr.decode()
