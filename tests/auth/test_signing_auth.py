# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``SigningAuth``: every request signed as it is sent, a caller's signature left alone, one re-sign on a 401."""

from __future__ import annotations

import itertools
import uuid
from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, override

import httpx2
import pytest

from headgate import HttpOptions, RetryPolicy, SigningAuth
from headgate._core import CredentialStamper
from headgate._url import is_secret_param
from headgate.errors import AuthError
from headgate.ratelimit import UNLIMITED
from headgate.testing import instant_retries
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import Callable

_BASE = "https://api.example"


class _Signer(SigningAuth):
    """Signs each request with a counter and the body it covers, so each signature differs from the last."""

    signed_headers: ClassVar = frozenset({"X-Signature", "X-Nonce"})

    def __init__(self) -> None:
        """Count from 1."""
        self._nonces = itertools.count(1)
        self.signed = 0

    @override
    def sign(self, request: httpx2.Request) -> None:
        self.signed += 1
        nonce = str(next(self._nonces))
        request.headers["X-Nonce"] = nonce
        request.headers["X-Signature"] = f"{nonce}:{request.method}:{request.url.path}:{request.content.decode()}"


class _PublicReads(_Signer):
    """Leaves the public ``/market/`` reads unsigned."""

    @override
    def skips(self, request: httpx2.Request) -> bool:
        return super().skips(request) or request.url.path.startswith("/market/")


class _QuerySigner(SigningAuth):
    """Signs into the query, as an exchange's ``signature`` parameter."""

    signed_params: ClassVar = frozenset({"signature"})

    @override
    def sign(self, request: httpx2.Request) -> None:
        request.url = request.url.copy_set_param("signature", "sig")


def _client(
    auth: httpx2.Auth, answer: Callable[[httpx2.Request], httpx2.Response]
) -> tuple[StubClient, list[httpx2.Request]]:
    """Return a client signing with *auth* over *answer*, and the list of the requests it sent, as sent."""
    sent: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        # A copy: the re-sign after a 401 signs the same request object again.
        sent.append(httpx2.Request(request.method, request.url, headers=request.headers.copy()))
        return answer(request)

    client = StubClient(
        base_url=_BASE, rate=UNLIMITED, auth=auth, http=HttpOptions(transport=httpx2.MockTransport(handle))
    )
    return client, sent


def _ok(_: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200)


def _unauthorized(_: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(httpx2.codes.UNAUTHORIZED)


async def test_every_request_is_signed_over_its_body() -> None:
    """Sign each call as it is sent, with the body read so the signature covers it."""
    auth = _Signer()
    client, sent = _client(auth, _ok)
    async with client:
        await client.post("orders", json={"qty": 1}, expect=RESPONSE)
        await client.get("orders/7", expect=RESPONSE)

    assert [request.headers["X-Signature"] for request in sent] == [
        '1:POST:/orders:{"qty":1}',
        "2:GET:/orders/7:",
    ]


async def test_a_request_the_caller_signed_is_left_alone() -> None:
    """Send a request already carrying any signed header as the caller wrote it."""
    auth = _Signer()
    client, sent = _client(auth, _ok)
    async with client:
        await client.get("orders", headers={"x-nonce": "mine"}, expect=RESPONSE)

    assert sent[0].headers["X-Nonce"] == "mine"
    assert "X-Signature" not in sent[0].headers
    assert auth.signed == 0


async def test_a_401_is_signed_again_once() -> None:
    """Re-sign a rejected request at the current time and send it once more; a second 401 reaches the caller."""
    auth = _Signer()
    client, sent = _client(auth, _unauthorized)
    async with client:
        with pytest.raises(AuthError):
            await client.get("orders", expect=RESPONSE)

    assert [request.headers["X-Nonce"] for request in sent] == ["1", "2"]


async def test_a_retry_that_went_stale_is_accepted_once_signed_again() -> None:
    """Accept the re-signed request after a retry re-sent the first signature into a 401."""
    auth = _Signer()
    answers = iter([httpx2.Response(503), httpx2.Response(httpx2.codes.UNAUTHORIZED), httpx2.Response(200)])
    with instant_retries():
        client, sent = _client(auth, lambda _: next(answers))
    async with client:
        response = await client.get("orders", expect=RESPONSE, retry=RetryPolicy(attempts=2))

    assert response.status_code == httpx2.codes.OK
    assert [request.headers["X-Nonce"] for request in sent] == ["1", "1", "2"]


async def test_a_401_to_a_request_it_skips_is_not_re_sent() -> None:
    """Send a request the subclass skips once, unsigned, whatever it is answered."""
    auth = _PublicReads()
    client, sent = _client(auth, _unauthorized)
    async with client:
        with pytest.raises(AuthError):
            await client.get("market/ticker", expect=RESPONSE)

    assert len(sent) == 1
    assert "X-Signature" not in sent[0].headers
    assert auth.signed == 0


async def test_an_untrusted_origins_401_is_not_re_signed() -> None:
    """Send a call to another origin once, its signature withheld by the credential guard, when that origin 401s."""
    auth = _Signer()
    client, sent = _client(auth, _unauthorized)
    async with client:
        with pytest.raises(AuthError):
            await client.get("https://cdn.example/x", expect=RESPONSE)

    assert len(sent) == 1
    assert not {"x-signature", "x-nonce"} & {name.lower() for name in sent[0].headers}
    assert auth.signed == 1


async def test_a_signed_parameter_is_declared_and_its_presence_skips() -> None:
    """Report the signed parameter, register it as secret, and leave a request already carrying it alone."""
    auth = _QuerySigner()
    client, sent = _client(auth, _ok)
    async with client:
        await client.get("orders", expect=RESPONSE)
        await client.get("orders", params={"signature": "mine"}, expect=RESPONSE)

    assert [request.url.params["signature"] for request in sent] == ["sig", "mine"]
    assert (auth.credential_params, auth.credential_headers) == ({"signature"}, frozenset())
    assert is_secret_param("signature")


def test_it_declares_its_headers_lower_cased_and_is_a_credential_stamper() -> None:
    """Report the signed headers lower-cased, as the guard and the cache compare them."""
    auth = _Signer()
    assert isinstance(auth, CredentialStamper)
    assert auth.credential_headers == {"x-signature", "x-nonce"}
    assert auth.credential_params == frozenset()


def test_each_instance_is_its_own_identity_unless_it_names_its_key() -> None:
    """Draw an identity once per instance, stable across calls, unless a subclass returns its key."""

    class _Keyed(_Signer):
        @override
        def credential_identity(self) -> bytes:
            return b"key"

    one, two = _Signer(), _Signer()
    assert one.credential_identity() == one.credential_identity()
    assert one.credential_identity() != two.credential_identity()
    assert _Keyed().credential_identity() == _Keyed().credential_identity() == b"key"


def test_a_signer_declaring_nothing_fails_when_it_is_defined() -> None:
    """Refuse a concrete subclass with no signed headers or parameters, but not an abstract one between."""

    class _Base(SigningAuth):
        @abstractmethod
        def key(self) -> str:
            """Return the key a subclass signs with."""

        @override
        def sign(self, request: httpx2.Request) -> None:
            request.headers["X-Key"] = self.key()

    with pytest.raises(TypeError, match="must declare the headers or parameters sign"):

        class _Undeclared(_Base):
            @override
            def key(self) -> str:
                return "k"

    class _Declared(_Base):
        signed_headers: ClassVar = frozenset({"X-Key"})
        signed_params: ClassVar = frozenset({f"headgate_test_{uuid.uuid4().hex}"})

        @override
        def key(self) -> str:
            return "k"

    assert _Declared().credential_headers == {"x-key"}
