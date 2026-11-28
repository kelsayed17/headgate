# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Which host the circuit breaker is keyed on, what a redirect to a bad URL becomes, and what precedes the breaker."""

from __future__ import annotations

import contextlib

import httpx2
import pytest

from headgate import (
    BreakerPolicy,
    CallPolicy,
    Client,
    ClientError,
    HttpOptions,
    Json,
    Rate,
    RetryPolicy,
    TransportError,
)
from headgate._responses import nothing
from headgate.breaker import BreakerState
from tests.support.breakers import failures, has_breaker, registry_of
from tests.support.responses import RESPONSE

_HOST = "api.example"


def _redirecting_to(location: str) -> httpx2.MockTransport:
    """Return a transport that answers ``/start`` with a 302 to *location*, and every other path with a 200."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return httpx2.Response(302, headers={"Location": location})
        return httpx2.Response(200, content=b"{}")

    return httpx2.MockTransport(handler)


def _open_breaker(client: Client) -> None:
    """Trip *client*'s breaker for the test host, built for the default policy: its threshold's worth of failures."""
    policy = BreakerPolicy()
    breaker = registry_of(client).breaker_for(f"https://{_HOST}")
    for _ in range(policy.failure_threshold):
        with contextlib.suppress(RuntimeError), breaker.track() as verdict:
            verdict.mark_failed()
            raise RuntimeError
    assert breaker.state is BreakerState.OPEN


# --- The host the breaker is keyed on ---


async def test_an_absolute_url_keys_the_breaker_on_its_own_host() -> None:
    """Key an absolute URL's failure on that URL's host, not the client's base URL."""
    client = Client(
        base_url=f"https://{_HOST}",
        policy=CallPolicy(rate=Rate.per_second(2), retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=httpx2.MockTransport(lambda _request: httpx2.Response(503))),
    )
    with pytest.raises(ClientError):
        await client.get("https://other.example/x", expect=RESPONSE)
    assert failures(client, "other.example") == 1
    assert not has_breaker(client, _HOST)


async def test_a_protocol_relative_path_keys_the_breaker_on_the_host_it_reaches() -> None:
    """Key a protocol-relative path on the host it reaches once joined onto the base URL.

    ``//other.example/x`` joins as ``https://api.example/x``, so that is the host to key.

    Keying from the path parsed alone would read ``other.example``: a host the request never reached would accumulate
    failures while the host it did reach recorded none.
    """
    reached: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        reached.append(request.url.host)
        return httpx2.Response(503)

    client = Client(
        base_url=f"https://{_HOST}",
        policy=CallPolicy(rate=Rate.per_second(2), retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=httpx2.MockTransport(handler)),
    )
    with pytest.raises(ClientError):
        await client.get("//other.example/x", expect=RESPONSE)
    assert reached == [_HOST]
    assert failures(client, _HOST) == 1
    assert not has_breaker(client, "other.example")


# --- A redirect to a bad URL ---


@pytest.mark.parametrize(
    "location",
    # A relative Location that parses alone but joins past httpx2's 65,536-character limit counts the same way.
    ["http://[::1", "/" + "b" * 65_530],
    ids=["unparsable", "joined-too-long"],
)
async def test_a_bad_redirect_location_is_a_transport_failure_of_the_host(location: str) -> None:
    """Count a bad ``Location`` against the host: httpx2 raises ``RemoteProtocolError``, an ``HTTPError``."""
    client = Client(
        base_url=f"https://{_HOST}",
        policy=CallPolicy(rate=Rate.per_second(2), retry=RetryPolicy(attempts=1)),
        http=HttpOptions(transport=_redirecting_to(location)),
    )
    with pytest.raises(TransportError, match="Invalid URL in location header"):
        await client.get("start", expect=RESPONSE)
    assert failures(client, _HOST) == 1


# --- The request is built before the breaker is asked ---


async def test_with_the_breaker_open_a_url_the_join_rejects_is_still_an_invalid_url() -> None:
    """Name a URL the base-URL join rejects as what it is, not hide it behind ``circuit breaker open``."""
    httpx2.URL("b" * 65_536)  # precondition: parses alone, so only the base-URL join can reject it
    client = Client(
        base_url=f"https://{_HOST}", policy=CallPolicy(rate=Rate.per_second(2), retry=RetryPolicy(attempts=1))
    )
    _open_breaker(client)
    with pytest.raises(ClientError, match="invalid URL") as caught:
        await client.get("b" * 65_536, expect=RESPONSE)
    assert not isinstance(caught.value, TransportError)


async def test_with_the_breaker_open_an_unencodable_body_raises_as_itself() -> None:
    """Fail to encode a body the same way with the breaker open: encoding is part of building the request."""
    client = Client(
        base_url=f"https://{_HOST}", policy=CallPolicy(rate=Rate.per_second(2), retry=RetryPolicy(attempts=1))
    )
    _open_breaker(client)
    with pytest.raises(TypeError, match="JSON serializable"):
        await client.post("x", expect=nothing, body=Json(object()))
