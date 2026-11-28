# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The flow of ``SigningAuth`` and ``CachedTokenAuth`` is not a hook: it holds their guarantees, so it is final.

Their hooks are ``sign``, ``skips``, ``attach`` and ``mint``; a per-request header belongs in a middleware. An override
of the flow passed ty and failed deep inside the call (E13), so the type checker reports it and the class statement
refuses it.
"""

from __future__ import annotations

import asyncio
import sys
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from headgate import CachedTokenAuth, SigningAuth

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _flow(_self: object, request: object) -> object:
    """Stand in for an override; never called."""
    return request


#: A declaration, so the class statement reaches the check of its final members.
_DECLARED = {"credential_headers": frozenset({"x-sig"})}


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
