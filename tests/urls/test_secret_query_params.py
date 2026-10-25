# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A secret query parameter never appears in a raised ``ClientError``'s message."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

from headgate import HttpOptions, QueryParamAuth, Rate, TransportPolicy
from headgate.errors import ClientError
from tests.support.clients import StubClient
from tests.support.responses import RESPONSE

if TYPE_CHECKING:
    from pytest_httpx2 import HTTPXMock

_CREDENTIAL_VALUE = "s3cr3t-value"
_RATE = Rate.per_second(100)
_BASE_URL = "https://api.example"


async def _raised(client: StubClient, param: str) -> ClientError:
    """Return the ``ClientError`` a GET carrying the credential in *param* raises."""
    with pytest.raises(ClientError) as info:
        await client.get("data", expect=RESPONSE, params={param: _CREDENTIAL_VALUE})
    return info.value


@pytest.mark.parametrize("param", ["apikey", "api_key", "token"])
@pytest.mark.parametrize("case", ["429", "500", "404", "transport"])
async def test_secret_query_param_never_in_error(httpx2_mock: HTTPXMock, param: str, case: str) -> None:
    """Keep the credential out of the message and the repr, for every way a request can fail."""
    client = StubClient(rate=_RATE, base_url=_BASE_URL, policy=TransportPolicy(max_attempts=2))
    if case == "transport":
        httpx2_mock.add_exception(httpx2.ConnectError("boom"), is_reusable=True)
    else:
        httpx2_mock.add_response(status_code=int(case), text="nope", is_reusable=True)
    error = await _raised(client, param)
    assert _CREDENTIAL_VALUE not in str(error)
    assert _CREDENTIAL_VALUE not in repr(error)


@pytest.mark.parametrize("param", ["access_token", "APIKEY", "api-key", "client_secret", "Signature"])
async def test_common_credential_names_are_redacted_whatever_their_case_or_separator(
    httpx2_mock: HTTPXMock, param: str
) -> None:
    """Redact a listed name whatever its case or separator."""
    client = StubClient(rate=_RATE, base_url=_BASE_URL, policy=TransportPolicy(max_attempts=1))
    httpx2_mock.add_response(status_code=429, text="slow", is_reusable=True)
    error = await _raised(client, param)
    assert _CREDENTIAL_VALUE not in str(error)


async def test_a_name_the_auth_declares_is_redacted_even_if_unlisted(httpx2_mock: HTTPXMock) -> None:
    """Redact a name a ``QueryParamAuth`` injects, though no list names it."""
    client = StubClient(
        rate=_RATE,
        base_url=_BASE_URL,
        policy=TransportPolicy(max_attempts=1),
        auth=QueryParamAuth({"zz_custom_credential": _CREDENTIAL_VALUE}),
    )
    httpx2_mock.add_response(status_code=503, text="down", is_reusable=True)
    with pytest.raises(ClientError) as info:
        await client.get("data", expect=RESPONSE)
    assert _CREDENTIAL_VALUE not in str(info.value)


async def test_a_transport_message_that_echoes_the_url_is_redacted() -> None:
    """Redact the credential from a transport exception's own message."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Fail the way httpx2 does, naming the request URL."""
        msg = f"bad response from {request.url}"
        raise httpx2.RemoteProtocolError(msg)

    client = StubClient(
        rate=_RATE,
        base_url=_BASE_URL,
        policy=TransportPolicy(max_attempts=1),
        http=HttpOptions(transport=httpx2.MockTransport(handler)),
    )
    with pytest.raises(ClientError) as info:
        await client.get("data", expect=RESPONSE, params={"apikey": _CREDENTIAL_VALUE})
    assert _CREDENTIAL_VALUE not in str(info.value)
