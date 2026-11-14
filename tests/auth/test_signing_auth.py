# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``SigningAuth``: every attempt signed afresh, a caller's signature left alone, one re-send on a 401."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, override

import httpx2
import pytest

from headgate import HttpCache, HttpOptions, Json, Next, RetryPolicy, SigningAuth, TransportPolicy
from headgate._core import CredentialStamper, Timing
from headgate._url import is_secret_param
from headgate.errors import AuthError
from headgate.ratelimit import UNLIMITED, Rate, RateBudget
from headgate.retry import Hedge
from headgate.testing import TestClock, instant_retries
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from hishel import AsyncSqliteStorage

_BASE = "https://api.example"
#: The sends a 401 costs: the signed request and its one re-signed re-send.
_SIGNED_AND_RE_SIGNED = 2


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
        await client.post("orders", body=Json({"qty": 1}), expect=RESPONSE)
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


async def test_each_attempt_is_signed_afresh() -> None:
    """Sign a retry again at the current time, so no two attempts carry the same signature."""
    auth = _Signer()
    answers = iter([httpx2.Response(503), httpx2.Response(200)])
    with instant_retries():
        client, sent = _client(auth, lambda _: next(answers))
    async with client:
        response = await client.get("orders", expect=RESPONSE, retry=RetryPolicy(attempts=2))

    assert response.status_code == httpx2.codes.OK
    assert [request.headers["X-Nonce"] for request in sent] == ["1", "2"]
    assert sent[0].headers["X-Signature"] != sent[1].headers["X-Signature"]


async def test_a_hedged_attempt_is_signed_afresh() -> None:
    """Sign a hedge sent beside a slow first attempt on its own, not with the first attempt's signature."""
    auth = _Signer()
    sent: list[httpx2.Headers] = []
    hedge_sent = asyncio.Event()

    async def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.headers.copy())
        if len(sent) == 1:
            await hedge_sent.wait()
        else:
            hedge_sent.set()
        return httpx2.Response(200)

    with TestClock().install():
        client = StubClient(
            base_url=_BASE, rate=UNLIMITED, auth=auth, http=HttpOptions(transport=httpx2.MockTransport(handle))
        )
    async with client:
        await client.get("orders", expect=RESPONSE, retry=RetryPolicy(budget=None, hedge=Hedge(after=0.5)))

    assert [headers["X-Nonce"] for headers in sent] == ["1", "2"]


async def test_a_cross_origin_hop_carries_no_signature() -> None:
    """Withhold the signature from a redirect hop to another origin, and send that origin's 401 no second time."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "other.test":
            return httpx2.Response(httpx2.codes.UNAUTHORIZED)
        return httpx2.Response(httpx2.codes.FOUND, headers={"Location": "https://other.test/x"})

    client, sent = _client(_Signer(), answer)
    async with client:
        with pytest.raises(AuthError):
            await client.get("orders", expect=RESPONSE)

    hops = [request for request in sent if request.url.host == "other.test"]
    assert len(hops) == 1
    assert "X-Signature" not in hops[0].headers


async def test_a_request_the_caller_signed_is_never_signed() -> None:
    """Send every attempt of a call that carries its own signature as the caller wrote it."""
    auth = _Signer()
    answers = iter([httpx2.Response(503), httpx2.Response(200)])
    with instant_retries():
        client, sent = _client(auth, lambda _: next(answers))
    async with client:
        await client.get("orders", headers={"X-Signature": "mine"}, expect=RESPONSE, retry=RetryPolicy(attempts=2))

    assert [request.headers["X-Signature"] for request in sent] == ["mine", "mine"]
    assert auth.signed == 0


class _Stamp:
    """A middleware stamping each attempt's number, which :class:`_StampSigner` covers."""

    credential_headers: ClassVar = frozenset[str]()
    credential_params: ClassVar = frozenset[str]()

    def __init__(self) -> None:
        """Count attempts from 1."""
        self._attempts = itertools.count(1)

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        request.headers["X-Stamp"] = f"a{next(self._attempts)}"
        return await call_next(request)


class _StampSigner(_Signer):
    """Signs the middleware's ``X-Stamp`` too, so it fails if it signs before the middleware ran."""

    @override
    def sign(self, request: httpx2.Request) -> None:
        super().sign(request)
        request.headers["X-Signature"] += f":{request.headers['X-Stamp']}"


async def test_each_attempt_is_signed_once_after_the_last_middleware() -> None:
    """Sign what the middleware stamped, once per attempt."""
    auth = _StampSigner()
    answers = iter([httpx2.Response(503), httpx2.Response(200)])

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.headers["X-Signature"])
        return next(answers)

    sent: list[str] = []
    http = HttpOptions(transport=httpx2.MockTransport(handle), middleware=(_Stamp(),))
    with instant_retries():
        client = StubClient(base_url=_BASE, rate=UNLIMITED, auth=auth, http=http)
    async with client:
        await client.get("orders", expect=RESPONSE, retry=RetryPolicy(attempts=2))

    assert sent == ["1:GET:/orders::a1", "2:GET:/orders::a2"]
    assert auth.signed == len(sent)


class _Wrapping(httpx2.Auth):
    """Runs a ``SigningAuth``'s flow inside its own, so the client's auth is not the signer."""

    def __init__(self, auth: SigningAuth) -> None:
        """Wrap *auth*."""
        self._auth = auth

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        flow = self._auth.async_auth_flow(request)
        yield await anext(flow)


async def test_a_signer_that_is_not_the_clients_auth_never_signs() -> None:
    """Send a request marked by a wrapped signer unsigned, middleware or none, so no undeclared signature leaves."""
    auth = _Signer()
    sent: list[httpx2.Headers] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.headers.copy())
        return httpx2.Response(200)

    http = HttpOptions(transport=httpx2.MockTransport(handle), middleware=(_Stamp(),))
    client = StubClient(base_url=_BASE, rate=UNLIMITED, auth=_Wrapping(auth), http=http)
    async with client:
        await client.get("orders", expect=RESPONSE)

    assert "X-Signature" not in sent[0]
    assert auth.signed == 0


class _Driving(httpx2.Auth):
    """Drives a ``SigningAuth``'s flow to its end, forwarding each response back, inside its own flow."""

    def __init__(self, auth: SigningAuth) -> None:
        """Wrap *auth*."""
        self._auth = auth

    @override
    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        flow = self._auth.async_auth_flow(request)
        step = await anext(flow)
        while True:
            response = yield step
            try:
                step = await flow.asend(response)
            except StopAsyncIteration:
                return


async def test_a_wrapped_signer_is_not_re_sent_on_a_401() -> None:
    """Send once, unsigned, a request whose signer is wrapped in another auth, when the server answers 401."""
    auth = _Signer()
    sent: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(httpx2.codes.UNAUTHORIZED)

    http = HttpOptions(transport=httpx2.MockTransport(handle), middleware=(_Stamp(),))
    client = StubClient(base_url=_BASE, rate=UNLIMITED, auth=_Driving(auth), http=http)
    async with client:
        with pytest.raises(AuthError):
            await client.get("orders", expect=RESPONSE)

    assert len(sent) == 1
    assert auth.signed == 0


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


class _ClockSigner(SigningAuth):
    """Signs each attempt with the time on *clock*, as a timestamp a server checks for freshness."""

    signed_headers: ClassVar = frozenset({"X-Signed-At"})

    def __init__(self, clock: TestClock) -> None:
        """Read the time from *clock*."""
        self._clock = clock

    @override
    def sign(self, request: httpx2.Request) -> None:
        request.headers["X-Signed-At"] = str(self._clock.now)


@pytest.mark.transport_policy
async def test_a_request_waiting_in_the_rate_queue_is_signed_when_it_leaves_it() -> None:
    """Sign after the rate wait, so a call queued behind a tight budget does not go out with a stale signature."""
    clock = TestClock()
    ages: list[float] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        ages.append(clock.now - float(request.headers["X-Signed-At"]))
        return httpx2.Response(200)

    budget = RateBudget(Rate.per_second(1), timing=Timing(clock=clock, sleep=clock.sleep), wall_clock=clock)
    with clock.install():
        client = StubClient(
            base_url=_BASE,
            rate=budget,
            auth=_ClockSigner(clock),
            http=HttpOptions(transport=httpx2.MockTransport(handle)),
        )
    async with client:
        await asyncio.gather(*(client.get("orders", expect=RESPONSE) for _ in range(3)))

    assert ages == [0.0, 0.0, 0.0]


@pytest.mark.parametrize("auth", [_Signer, _QuerySigner], ids=["header", "query"])
async def test_a_cached_clients_401_is_signed_again_once(auth: type[SigningAuth], storage: AsyncSqliteStorage) -> None:
    """Re-sign a 401 behind the cache too, which hands the request down below the signature and back."""
    sent: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(httpx2.codes.UNAUTHORIZED)

    client = StubClient(
        base_url=_BASE,
        rate=UNLIMITED,
        auth=auth(),
        policy=TransportPolicy(cache=HttpCache(storage=storage)),
        http=HttpOptions(transport=httpx2.MockTransport(handle)),
    )
    async with client:
        with pytest.raises(AuthError):
            await client.get("orders", expect=RESPONSE)

    assert len(sent) == _SIGNED_AND_RE_SIGNED
