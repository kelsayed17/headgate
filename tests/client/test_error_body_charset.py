# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""An error response's body is read as text whatever ``charset`` it declares, so its error stays a ``ClientError``.

httpx2 takes any codec Python registers as a response's encoding, among them bytes-to-bytes ones (``base64``, ``hex``,
``zlib``) and the text-to-text ``rot13``, which ``bytes.decode`` refuses with a ``LookupError``, and ``idna`` and
``undefined``, which refuse with a ``UnicodeError``.
"""

from __future__ import annotations

import httpx2
import pytest

from headgate import Client, HttpOptions, RetryPolicy, TransportPolicy, UpstreamStatusError, as_bytes
from headgate._failures import body_snippet

#: Codecs Python registers that are not text encodings.
_NOT_TEXT = ["base64", "hex", "rot13", "zlib", "idna", "undefined", "no-such-charset"]


@pytest.mark.parametrize("charset", _NOT_TEXT)
def test_a_body_in_a_charset_that_is_no_text_encoding_reads_as_utf_8(charset: str) -> None:
    """Read the body as UTF-8, replacing what is not, as for a body that declares no charset."""
    resp = httpx2.Response(404, headers={"Content-Type": f"text/plain; charset={charset}"}, content="no café".encode())
    assert body_snippet(resp) == "no café"


#: What a status error ends its first line with: the status, then the body, read as UTF-8.
_NAMES_THE_BODY = r"\(status=\d+\) — gone\n"

_STATUSES = [httpx2.codes.NOT_FOUND, httpx2.codes.SERVICE_UNAVAILABLE]


#: A charset of each way a codec refuses: a ``LookupError``, and a ``UnicodeError``.
_REFUSING = ["base64", "idna"]


def _client(status: int, charset: str) -> Client:
    """Return a client answering every request once with *status* and a body declared in *charset*."""
    headers = {"Content-Type": f"text/plain; charset={charset}"}
    return Client(
        base_url="https://api.example.test",
        policy=TransportPolicy(retry=RetryPolicy(attempts=1)),
        http=HttpOptions(
            transport=httpx2.MockTransport(lambda _request: httpx2.Response(status, headers=headers, content=b"gone"))
        ),
    )


async def _open_stream(client: Client) -> None:
    """Open a stream of ``GET x`` on *client*, and close it."""
    async with client.stream("x"):
        pass


@pytest.mark.parametrize("charset", _REFUSING)
@pytest.mark.parametrize("status", _STATUSES)
async def test_an_error_in_a_charset_that_is_no_text_encoding_is_a_status_error(status: int, charset: str) -> None:
    """Raise the status error, its body named, rather than the error the charset's codec would raise."""
    with pytest.raises(UpstreamStatusError, match=_NAMES_THE_BODY):
        await _client(status, charset).get("x", expect=as_bytes)


@pytest.mark.parametrize("charset", _REFUSING)
@pytest.mark.parametrize("status", _STATUSES)
async def test_a_streamed_error_in_a_charset_that_is_no_text_encoding_is_a_status_error(
    status: int, charset: str
) -> None:
    """Raise the status error from a stream's head too."""
    with pytest.raises(UpstreamStatusError, match=_NAMES_THE_BODY):
        await _open_stream(_client(status, charset))
