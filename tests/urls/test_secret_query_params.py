"""A secret query parameter never appears in a raised ``ClientError``'s message."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import QueryParamAuth
from headgate._rate_limit import Rate
from headgate.errors import ClientError
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx2 import HTTPXMock

_SECRET = "s3cr3t-value"


async def _raised(client: StubClient, param: str) -> ClientError:
    with pytest.raises(ClientError) as info:
        await client.get("data", expect=RESPONSE, params={param: _SECRET})
    return info.value


@pytest.mark.parametrize("param", ["apikey", "api_key", "token"])
@pytest.mark.parametrize("case", ["429", "500", "404", "transport"])
async def test_secret_query_param_never_in_error(httpx2_mock: HTTPXMock, param: str, case: str) -> None:
    client = StubClient(rate=Rate.per_second(100), base_url="https://api.example", max_attempts=2)
    if case == "transport":
        httpx2_mock.add_exception(httpx2.ConnectError("boom"), is_reusable=True)
    else:
        httpx2_mock.add_response(status_code=int(case), text="nope", is_reusable=True)
    error = await _raised(client, param)
    assert _SECRET not in str(error)
    assert _SECRET not in repr(error)


@pytest.mark.parametrize("param", ["access_token", "APIKEY", "api-key", "client_secret", "Signature"])
async def test_common_credential_names_are_redacted_whatever_their_case_or_separator(
    httpx2_mock: HTTPXMock, param: str
) -> None:
    client = StubClient(rate=Rate.per_second(100), base_url="https://api.example", max_attempts=1)
    httpx2_mock.add_response(status_code=429, text="slow", is_reusable=True)
    error = await _raised(client, param)
    assert _SECRET not in str(error)


async def test_a_name_the_auth_declares_is_redacted_even_if_unlisted(httpx2_mock: HTTPXMock) -> None:
    client = StubClient(
        rate=Rate.per_second(100),
        base_url="https://api.example",
        max_attempts=1,
        auth=QueryParamAuth({"zz_custom_credential": _SECRET}),
    )
    httpx2_mock.add_response(status_code=503, text="down", is_reusable=True)
    with pytest.raises(ClientError) as info:
        await client.get("data", expect=RESPONSE)
    assert _SECRET not in str(info.value)


async def test_a_transport_message_that_echoes_the_url_is_redacted() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.RemoteProtocolError(f"bad response from {request.url}")

    client = StubClient(
        rate=Rate.per_second(100),
        base_url="https://api.example",
        max_attempts=1,
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(ClientError) as info:
        await client.get("data", expect=RESPONSE, params={"apikey": _SECRET})
    assert _SECRET not in str(info.value)
