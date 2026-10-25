"""How the circuit breaker is keyed, and what a redirect to a bad URL becomes.

The first half characterises the pipeline as it stands; the tests below the second heading pin three deliberate
behaviour changes.
"""

from __future__ import annotations

import contextlib

import httpx2
import pytest

from headgate._base import AsyncBaseClient
from headgate._breaker import HOST_BREAKERS, AsyncCircuitBreaker, BreakerState
from headgate._rate_limit import Rate
from headgate._responses import nothing
from headgate.errors import ClientError, TransportError
from tests.support.responses import RESPONSE

_HOST = "api.example"


class _Client(AsyncBaseClient):
    """A bare client: the base's policy and nothing of its own."""


def _redirecting_to(location: str) -> httpx2.MockTransport:
    """``/start`` answers 302 to *location*; every other path answers 200."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return httpx2.Response(302, headers={"Location": location})
        return httpx2.Response(200, content=b"{}")

    return httpx2.MockTransport(handler)


# ---------- Characterisation: unchanged by the refactor ----------


async def test_an_unparseable_redirect_location_is_a_transport_failure_of_the_host() -> None:
    """httpx2 wraps a ``Location`` that does not parse as ``RemoteProtocolError``, an ``HTTPError``: a host failure."""
    client = _Client(
        rate=Rate.per_second(2), base_url=f"https://{_HOST}", transport=_redirecting_to("http://[::1"), max_retries=1
    )
    with pytest.raises(TransportError, match="Invalid URL in location header"):
        await client.get("start", expect=RESPONSE)
    assert HOST_BREAKERS[_HOST].failure_count == 1


async def test_a_redirect_whose_joined_url_is_too_long_is_a_client_error_not_a_host_failure() -> None:
    """A relative ``Location`` that parses alone but pushes the JOINED URL past httpx2's 65,536-character limit.

    httpx2 does not wrap that one: it raises a bare ``InvalidURL``, which is not an ``HTTPError``, so unless the client
    maps it, it reaches the application boundary as a 500 instead of a 502. It says nothing about the host.
    """
    client = _Client(
        rate=Rate.per_second(2),
        base_url=f"https://{_HOST}",
        transport=_redirecting_to("/" + "b" * 65_530),
        max_retries=1,
    )
    with pytest.raises(ClientError, match="invalid URL") as caught:
        await client.get("start", expect=RESPONSE)
    assert not isinstance(caught.value, TransportError)
    assert HOST_BREAKERS[_HOST].failure_count == 0


async def test_an_absolute_url_keys_the_breaker_on_its_own_host() -> None:
    client = _Client(
        rate=Rate.per_second(2),
        base_url=f"https://{_HOST}",
        transport=httpx2.MockTransport(lambda _request: httpx2.Response(503)),
        max_retries=1,
    )
    with pytest.raises(ClientError):
        await client.get("https://other.example/x", expect=RESPONSE)
    assert HOST_BREAKERS["other.example"].failure_count == 1
    assert _HOST not in HOST_BREAKERS


# ---------- The three behaviours the refactor changes (spec §9) ----------


def _open_breaker() -> None:
    """Trip the test host's breaker: one recorded failure against a threshold of one."""
    breaker = AsyncCircuitBreaker(failure_threshold=1, recovery_timeout=3600, name=_HOST)
    HOST_BREAKERS[_HOST] = breaker
    with contextlib.suppress(RuntimeError), breaker.track() as verdict:
        verdict.host_failed()
        raise RuntimeError
    assert breaker.state is BreakerState.OPEN


async def test_a_protocol_relative_path_keys_the_breaker_on_the_host_it_reaches() -> None:
    """``//other.example/x`` joins onto the base URL as ``https://api.example/x``, so that is the host to key.

    Previously the breaker was keyed from the path parsed ALONE, which read ``other.example``: a host the
    request never reached accumulated failures while the host it did reach recorded none.
    """
    reached: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        reached.append(request.url.host)
        return httpx2.Response(503)

    client = _Client(
        rate=Rate.per_second(2), base_url=f"https://{_HOST}", transport=httpx2.MockTransport(handler), max_retries=1
    )
    with pytest.raises(ClientError):
        await client.get("//other.example/x", expect=RESPONSE)
    assert reached == [_HOST]
    assert HOST_BREAKERS[_HOST].failure_count == 1
    assert "other.example" not in HOST_BREAKERS


async def test_with_the_breaker_open_a_url_the_join_rejects_is_still_an_invalid_url() -> None:
    """The request is built before the breaker is asked, so a URL the join rejects is named as what it is."""
    httpx2.URL("b" * 65_536)  # precondition: parses alone, so only the base-URL join can reject it
    _open_breaker()
    client = _Client(rate=Rate.per_second(2), base_url=f"https://{_HOST}", max_retries=1)
    with pytest.raises(ClientError, match="invalid URL") as caught:
        await client.get("b" * 65_536, expect=RESPONSE)
    assert not isinstance(caught.value, TransportError)


async def test_with_the_breaker_open_an_unencodable_body_raises_as_itself() -> None:
    """Encoding the body is part of building the request, which now precedes the breaker."""
    _open_breaker()
    client = _Client(rate=Rate.per_second(2), base_url=f"https://{_HOST}", max_retries=1)
    with pytest.raises(TypeError, match="JSON serializable"):
        await client.post("x", expect=nothing, json=object())
