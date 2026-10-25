"""Which host the circuit breaker is keyed on, what a redirect to a bad URL becomes, and what precedes the breaker."""

from __future__ import annotations

import contextlib

import httpx2
import pytest

from headgate._breaker import HOST_BREAKERS, BreakerState, CircuitBreaker
from headgate._rate_limit import Rate
from headgate._responses import nothing
from headgate.errors import ClientError, TransportError
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

_HOST = "api.example"


def _redirecting_to(location: str) -> httpx2.MockTransport:
    """``/start`` answers 302 to *location*; every other path answers 200."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return httpx2.Response(302, headers={"Location": location})
        return httpx2.Response(200, content=b"{}")

    return httpx2.MockTransport(handler)


def _open_breaker() -> None:
    """Trip the test host's breaker: one recorded failure against a threshold of one."""
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=3600, name=_HOST)
    HOST_BREAKERS[_HOST] = breaker
    with contextlib.suppress(RuntimeError), breaker.track() as verdict:
        verdict.mark_failed()
        raise RuntimeError
    assert breaker.state is BreakerState.OPEN


# --- The host the breaker is keyed on ---


async def test_an_absolute_url_keys_the_breaker_on_its_own_host() -> None:
    client = StubClient(
        rate=Rate.per_second(2),
        base_url=f"https://{_HOST}",
        transport=httpx2.MockTransport(lambda _request: httpx2.Response(503)),
        max_attempts=1,
    )
    with pytest.raises(ClientError):
        await client.get("https://other.example/x", expect=RESPONSE)
    assert HOST_BREAKERS["other.example"].failure_count == 1
    assert _HOST not in HOST_BREAKERS


async def test_a_protocol_relative_path_keys_the_breaker_on_the_host_it_reaches() -> None:
    """``//other.example/x`` joins onto the base URL as ``https://api.example/x``, so that is the host to key.

    Keying from the path parsed ALONE would read ``other.example``: a host the request never reached would accumulate
    failures while the host it did reach recorded none.
    """
    reached: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        reached.append(request.url.host)
        return httpx2.Response(503)

    client = StubClient(
        rate=Rate.per_second(2), base_url=f"https://{_HOST}", transport=httpx2.MockTransport(handler), max_attempts=1
    )
    with pytest.raises(ClientError):
        await client.get("//other.example/x", expect=RESPONSE)
    assert reached == [_HOST]
    assert HOST_BREAKERS[_HOST].failure_count == 1
    assert "other.example" not in HOST_BREAKERS


# --- A redirect to a bad URL ---


async def test_an_unparseable_redirect_location_is_a_transport_failure_of_the_host() -> None:
    """httpx2 wraps a ``Location`` that does not parse as ``RemoteProtocolError``, an ``HTTPError``: a host failure."""
    client = StubClient(
        rate=Rate.per_second(2), base_url=f"https://{_HOST}", transport=_redirecting_to("http://[::1"), max_attempts=1
    )
    with pytest.raises(TransportError, match="Invalid URL in location header"):
        await client.get("start", expect=RESPONSE)
    assert HOST_BREAKERS[_HOST].failure_count == 1


async def test_a_redirect_whose_joined_url_is_too_long_is_a_client_error_not_a_host_failure() -> None:
    """A relative ``Location`` that parses alone but pushes the JOINED URL past httpx2's 65,536-character limit.

    httpx2 does not wrap that one: it raises a bare ``InvalidURL``, which is not an ``HTTPError``, so the client maps it
    to a ``ClientError`` itself. It says nothing about the host.
    """
    client = StubClient(
        rate=Rate.per_second(2),
        base_url=f"https://{_HOST}",
        transport=_redirecting_to("/" + "b" * 65_530),
        max_attempts=1,
    )
    with pytest.raises(ClientError, match="invalid URL") as caught:
        await client.get("start", expect=RESPONSE)
    assert not isinstance(caught.value, TransportError)
    assert HOST_BREAKERS[_HOST].failure_count == 0


# --- The request is built before the breaker is asked ---


async def test_with_the_breaker_open_a_url_the_join_rejects_is_still_an_invalid_url() -> None:
    """A URL the base-URL join rejects is named as what it is, not hidden behind ``circuit breaker open``."""
    httpx2.URL("b" * 65_536)  # precondition: parses alone, so only the base-URL join can reject it
    _open_breaker()
    client = StubClient(rate=Rate.per_second(2), base_url=f"https://{_HOST}", max_attempts=1)
    with pytest.raises(ClientError, match="invalid URL") as caught:
        await client.get("b" * 65_536, expect=RESPONSE)
    assert not isinstance(caught.value, TransportError)


async def test_with_the_breaker_open_an_unencodable_body_raises_as_itself() -> None:
    """Encoding the body is part of building the request, so it fails the same way with the breaker open."""
    _open_breaker()
    client = StubClient(rate=Rate.per_second(2), base_url=f"https://{_HOST}", max_attempts=1)
    with pytest.raises(TypeError, match="JSON serializable"):
        await client.post("x", expect=nothing, json=object())
