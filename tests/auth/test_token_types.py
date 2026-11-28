# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A ``CachedTokenAuth`` whose token is not text attaches it itself: the default never sends a token's ``repr``.

The default ``attach`` sends ``Authorization: Bearer <token>``, which is right only for a ``str``. A subclass whose
token type, read from its bases, is anything else must override ``attach`` when it is defined; when the type cannot be
read there (a generic subclass bound further down), the default refuses a token that is not a ``str`` at the first
call, before anything is sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, override

import pytest

from headgate import CachedTokenAuth, Client, HttpOptions, as_json
from headgate.testing import FakeTransport, Respond

if TYPE_CHECKING:
    import httpx2

_TOKEN = {"access": "abc", "refresh": "r"}


def _define_struct_token() -> type:
    class StructToken(CachedTokenAuth[dict[str, str]]):
        @override
        async def mint_token(self) -> tuple[dict[str, str], float]:
            return _TOKEN, 3600.0

    return StructToken


def test_a_struct_token_without_attach_is_refused_when_defined() -> None:
    """Refuse the class, naming its token type, rather than send the token's repr as a bearer."""
    with pytest.raises(TypeError, match=r"StructToken holds a dict\[str, str\] token, so it must override attach"):
        _define_struct_token()


class _Mid[T](CachedTokenAuth[T]):
    """A generic layer whose token type its subclasses choose: unreadable where the class is defined."""

    def __init__(self, token: T) -> None:
        """Mint *token* every time."""
        super().__init__()
        self._token = token

    @override
    async def mint_token(self) -> tuple[T, float]:
        return self._token, 3600.0


class _Leaf(_Mid[dict[str, str]]):
    """Mints a struct token and keeps the default attach."""


async def test_a_generic_subclass_is_refused_at_the_first_attach() -> None:
    """Refuse a token that is not text when it is attached, before the request is sent."""
    fake = FakeTransport().reply("GET", "/x", Respond(json={}))
    client = Client(base_url="https://api.example", auth=_Leaf(_TOKEN), http=HttpOptions(transport=fake))
    async with client:
        with pytest.raises(TypeError, match=r"_Leaf holds a dict token, which the default attach cannot send"):
            await client.get("x", expect=as_json)

    assert fake.calls == ()


class _StructWithAttach(CachedTokenAuth[dict[str, str]]):
    """Mints a struct token and attaches its access half."""

    token_headers: ClassVar = frozenset({"Authorization"})

    @override
    async def mint_token(self) -> tuple[dict[str, str], float]:
        return _TOKEN, 3600.0

    @override
    def attach(self, request: httpx2.Request, token: dict[str, str]) -> None:
        request.headers["Authorization"] = f"Bearer {token['access']}"


async def test_a_struct_token_with_attach_is_accepted() -> None:
    """Send what the override attaches."""
    fake = FakeTransport().reply("GET", "/x", Respond(json={}))
    async with Client(base_url="https://api.example", auth=_StructWithAttach(), http=HttpOptions(transport=fake)) as c:
        await c.get("x", expect=as_json)

    assert fake.calls[0].headers["Authorization"] == "Bearer abc"


class _AnyToken(CachedTokenAuth[Any]):
    """A token typed ``Any``, which says nothing about whether the default attach can send it."""

    @override
    async def mint_token(self) -> tuple[Any, float]:
        return "abc", 3600.0


class _MaybeToken(CachedTokenAuth[str | None]):
    """A token typed ``str | None``: a ``str`` when it is minted, so the default attach sends it."""

    @override
    async def mint_token(self) -> tuple[str | None, float]:
        return "abc", 3600.0


@pytest.mark.parametrize("auth", [_AnyToken(), _MaybeToken()], ids=["any", "optional-str"])
async def test_a_token_type_that_may_be_text_keeps_the_default_attach(auth: CachedTokenAuth[Any]) -> None:
    """Accept ``Any`` and ``str | None`` when the class is defined, and send a ``str`` token they mint."""
    fake = FakeTransport().reply("GET", "/x", Respond(json={}))
    async with Client(base_url="https://api.example", auth=auth, http=HttpOptions(transport=fake)) as client:
        await client.get("x", expect=as_json)

    assert fake.calls[0].headers["Authorization"] == "Bearer abc"


async def test_a_seeded_token_that_is_not_text_is_refused_without_saying_minted() -> None:
    """Refuse a seeded token the default attach cannot send, in words that fit a token it did not mint."""
    auth = _Leaf(_TOKEN)
    auth.seed(_TOKEN, 3600.0)
    fake = FakeTransport().reply("GET", "/x", Respond(json={}))
    async with Client(base_url="https://api.example", auth=auth, http=HttpOptions(transport=fake)) as client:
        with pytest.raises(TypeError, match=r"^_Leaf holds a dict token") as raised:
            await client.get("x", expect=as_json)

    assert "minted" not in str(raised.value)
