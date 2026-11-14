# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What an auth's one re-send on a 401 costs: the 401's body is not read, and the call's own body is not buffered.

httpx2 reads in full every response an auth flow answers with another request, below the client's
``max_response_bytes`` cap, so an auth drops the 401's body before re-sending, as a followed redirect's is dropped. A
``CachedTokenAuth`` under a headgate client does not buffer the call's body for that re-send either: a body that can be
sent again (bytes, a factory) is re-sent, and a one-shot stream is not, so a large upload is never held in memory.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, override

import httpx2
import pytest

from headgate import CachedTokenAuth, HttpOptions, SigningAuth
from headgate._responses import nothing
from headgate.errors import AuthError
from headgate.ratelimit import UNLIMITED
from tests.support.clients import StubClient
from tests.support.tokens import premint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

_BASE = "https://api.example"
#: The client's response cap in these tests, far below the 401 body.
_CAP = 1024
#: The 401 body's chunks, each one cap's worth.
_CHUNKS = 64


class _Signer(SigningAuth):
    """Signs with a fixed header."""

    signed_headers: ClassVar = frozenset({"X-Signature"})

    @override
    def sign(self, request: httpx2.Request) -> None:
        request.headers["X-Signature"] = "sig"


class _Minting(CachedTokenAuth):
    """Mints ``fresh`` whenever asked; tests seed it a stale token first."""

    def __init__(self) -> None:
        """Count the mints."""
        super().__init__()
        self.mints = 0

    @override
    async def mint_token(self) -> tuple[str, float]:
        self.mints += 1
        return "fresh", 3600.0


class _Counted(httpx2.AsyncByteStream):
    """A body of *chunks* chunks of one cap each, counting the chunks read from it."""

    def __init__(self, chunks: int) -> None:
        """Nothing read yet."""
        self.read = 0
        self._chunks = chunks

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.read += 1
            yield b"x" * _CAP


class _Network(httpx2.AsyncBaseTransport):
    """Answers the first request 401 with a large body, and the rest *final* with a small one; records what it got."""

    def __init__(self, *, final: int = httpx2.codes.UNAUTHORIZED, large: bool = True) -> None:
        """Answer the re-send with *final*, and the first request with a large body unless *large* is false."""
        self.bodies: list[_Counted] = []
        self.sent: list[bytes] = []
        self.bearers: list[str | None] = []
        self.produced_before_send: list[int] = []
        self._final = final
        self._large = large

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.produced_before_send.append(len(_PRODUCED))
        self.sent.append(await request.aread())
        self.bearers.append(request.headers.get("Authorization"))
        status = httpx2.codes.UNAUTHORIZED if not self.bodies else self._final
        body = _Counted(_CHUNKS if self._large and not self.bodies else 0)
        self.bodies.append(body)
        return httpx2.Response(status, stream=body)


#: The chunks a test's one-shot upload has produced so far.
_PRODUCED: list[bytes] = []


@pytest.fixture(autouse=True)
def _nothing_produced() -> None:
    _PRODUCED.clear()


def _client(auth: httpx2.Auth, network: _Network) -> StubClient:
    return StubClient(
        base_url=_BASE,
        rate=UNLIMITED,
        auth=auth,
        http=HttpOptions(transport=network, max_response_bytes=_CAP),
    )


@pytest.mark.parametrize("auth", [_Signer, lambda: premint(_Minting(), "stale")], ids=["signing", "cached-token"])
async def test_the_401_an_auth_re_sends_after_is_not_read(auth: Callable[[], httpx2.Auth]) -> None:
    """Drop the first 401's body unread before the re-send, instead of reading it in full past the cap."""
    network = _Network()
    async with _client(auth(), network) as client:
        with pytest.raises(AuthError):
            await client.get("orders", expect=nothing)

    assert network.bodies[0].read == 0


async def _upload() -> AsyncIterator[bytes]:
    for chunk in (b"part-1,", b"part-2"):
        _PRODUCED.append(chunk)
        yield chunk


async def test_a_token_auth_streams_a_one_shot_upload_and_does_not_re_send_it() -> None:
    """Send a one-shot stream as it is produced, and answer its 401 without a re-send, forgetting the stale token."""
    auth = premint(_Minting(), "stale")
    network = _Network(large=False)
    async with _client(auth, network) as client:
        with pytest.raises(AuthError):
            await client.post("orders", content=_upload(), expect=nothing)
        with pytest.raises(AuthError):
            await client.get("orders", expect=nothing)

    assert network.produced_before_send[0] == 0, "nothing was buffered before the send"
    assert network.sent[0] == b"part-1,part-2"
    assert network.bearers == ["Bearer stale", "Bearer fresh"], "no re-send; the next call minted in its place"


async def test_a_token_auth_re_sends_a_body_built_by_a_factory_in_full() -> None:
    """Re-send a factory's body after the re-mint, built afresh, since it can be sent again."""
    network = _Network(final=httpx2.codes.OK)
    async with _client(premint(_Minting(), "stale"), network) as client:
        await client.post("orders", content=_upload, expect=nothing)

    assert network.sent == [b"part-1,part-2", b"part-1,part-2"]


class _HoldsAnswers(_Network):
    """Answers 401, but only once *count* requests have arrived, so they are all sent before any answer is read."""

    def __init__(self, count: int) -> None:
        """Hold the answers until *count* requests arrived."""
        super().__init__(large=False)
        self._count = count
        self._arrived = asyncio.Event()

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent.append(await request.aread())
        if len(self.sent) == self._count:
            self._arrived.set()
        await self._arrived.wait()
        return httpx2.Response(httpx2.codes.UNAUTHORIZED)


async def test_concurrent_one_shot_uploads_rejected_together_each_reach_the_caller() -> None:
    """Answer each one-shot upload's 401 as itself, the second finding the stale token already dropped."""
    auth = premint(_Minting(), "stale")
    network = _HoldsAnswers(2)
    async with _client(auth, network) as client:
        results = await asyncio.gather(
            *(client.post("orders", content=_upload(), expect=nothing) for _ in range(2)), return_exceptions=True
        )

    assert [type(result) for result in results] == [AuthError, AuthError]
    assert auth.mints == 0
