# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A credential declaration cannot split into letters: ``frozenset("X-Api-Key")`` is refused wherever names are read.

``frozenset("X-Api-Key")`` is a ``frozenset[str]``, so it type-checks, but it declares eight one-letter names and
withholds no real header. No real header or query credential is one character long, so a non-empty declaration of
nothing but one-character names is refused: on a middleware when the client is built, on an auth when its class is
defined, and on a ``CassetteFilter`` when it is built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast, override

import httpx2
import pytest

from headgate import Auth, Client, HttpOptions, SigningAuth, as_json
from headgate.testing import CassetteFilter, FakeTransport, Respond

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from headgate import Next

_LETTERS = r"splits a name into its letters: write frozenset\(\{'X-Api-Key'\}\)"


class _SplitSigner:
    """A middleware whose declaration was meant to be ``frozenset({"X-Api-Key"})``."""

    credential_headers: ClassVar = frozenset("X-Api-Key")
    credential_params: ClassVar = frozenset[str]()

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        request.headers["X-Api-Key"] = "secret"
        return await call_next(request)


def test_a_middleware_declaring_letters_is_refused() -> None:
    """Refuse the middleware when the client is built, naming it and the spelling to write."""
    with pytest.raises(TypeError, match=_LETTERS) as raised:
        Client(base_url="https://api.example", middleware=(_SplitSigner(),))
    assert "_SplitSigner.credential_headers" in str(raised.value)


def _split_auth() -> type:
    class SplitAuth(Auth):
        credential_headers: ClassVar = frozenset("X-Api-Key")
        credential_params: ClassVar = frozenset[str]()

        @override
        def credential_identity(self) -> bytes:
            return b"k"

        @override
        async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
            yield request

    return SplitAuth


def _split_signer() -> type:
    class SplitSigner(SigningAuth):
        credential_headers: ClassVar = frozenset("X-Api-Key")

        @override
        def sign(self, request: httpx2.Request) -> None:
            request.headers["X-Api-Key"] = "sig"

    return SplitSigner


def _not_a_frozenset() -> type:
    class Listed(Auth):
        credential_headers: ClassVar = ["x-api-key"]
        credential_params: ClassVar = frozenset[str]()

        @override
        def credential_identity(self) -> bytes:
            return b"k"

    return Listed


@pytest.mark.parametrize("define", [_split_auth, _split_signer], ids=["auth", "signing-auth"])
def test_an_auth_declaring_letters_is_refused_when_defined(define: Callable[[], type]) -> None:
    """Refuse the class itself, before any client is built with it."""
    with pytest.raises(TypeError, match=_LETTERS):
        define()


def test_an_auth_declaring_a_list_is_refused_when_defined() -> None:
    """Refuse a class attribute that is not a frozenset of names when the class is defined."""
    with pytest.raises(TypeError, match=r"Listed\.credential_headers must be a frozenset of names, not list"):
        _not_a_frozenset()


def test_a_cassette_filter_refuses_letters() -> None:
    """Refuse names the filter would scrub one letter at a time, so the credential would be recorded."""
    with pytest.raises(TypeError, match=_LETTERS):
        CassetteFilter(headers=frozenset("X-Api-Key"))


#: An auth declaring a split name through a property, which only an instance reads.
_SplitByProperty = type(
    "_SplitByProperty",
    (Auth,),
    {
        "credential_headers": property(lambda _self: frozenset("X-Api-Key")),
        "credential_params": property(lambda _self: frozenset[str]()),
        "credential_identity": lambda _self: b"k",
    },
)


def test_a_cassette_filter_refuses_an_auth_declaring_letters() -> None:
    """Refuse an auth whose declaration splits, as the client would."""
    with pytest.raises(TypeError, match=_LETTERS):
        CassetteFilter(auths=(cast("Any", _SplitByProperty)(),))


def test_a_client_refuses_an_auth_declaring_letters_through_a_property() -> None:
    """Refuse, when the client is built, a split declaration the class definition could not see."""
    with pytest.raises(TypeError, match=_LETTERS):
        Client(base_url="https://api.example", auth=cast("Any", _SplitByProperty)())


async def test_one_real_one_character_name_beside_others_passes() -> None:
    """Accept a declaration with a one-character name among longer ones: only a set of letters is a split name."""

    class Signer:
        credential_headers: ClassVar = frozenset({"x", "x-api-key"})
        credential_params: ClassVar = frozenset[str]()

        async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
            request.headers["X-Api-Key"] = "secret"
            return await call_next(request)

    fake = FakeTransport().reply("GET", "/x", Respond(json={}))
    client = Client(base_url="https://api.example", middleware=(Signer(),), http=HttpOptions(transport=fake))
    async with client:
        await client.get(httpx2.URL("https://other.example/x"), expect=as_json)

    assert "x-api-key" not in fake.calls[0].headers


def test_a_split_parameter_shows_a_parameter_spelling() -> None:
    """Show a query parameter's spelling for a split ``credential_params``, not a header's."""
    with pytest.raises(TypeError, match=r"write frozenset\(\{'api_key'\}\)"):
        CassetteFilter(params=frozenset("api_key"))


def test_a_lone_one_character_name_is_refused_in_its_own_words() -> None:
    """Refuse a single one-character name, saying no credential is one character long, not that a name was split."""
    with pytest.raises(TypeError, match=r"names only 'x', one character long, which no header or query credential is"):
        CassetteFilter(headers=["x"])
