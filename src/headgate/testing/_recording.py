# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Recorded-fixture hygiene: keep credentials and token mints out of vcrpy / pytest-recording cassettes.

:class:`CassetteFilter` builds a vcrpy configuration from what headgate already knows is secret: the credential
headers, the query parameters :func:`headgate._url.is_secret_param` names (the well-known ones and every name a
``QueryParamAuth`` registered), and what each auth declares through
:class:`~headgate._core.CredentialStamper`. It is also a :class:`~headgate.Middleware` that marks the requests an
auth flow sends to mint a credential (:data:`headgate._auth.AUTH_HOP`), so the recorder skips them: a token response
is a credential, and not the endpoint's answer.

The recipe, with pytest-recording::

    CASSETTES = CassetteFilter(auths=(my_auth,))

    @pytest.fixture(scope="module")
    def vcr_config() -> dict[str, object]:
        return CASSETTES.vcr_config()

and ``HttpOptions(middleware=(CASSETTES,))`` on the client under test. A skipped mint is not replayed, so seed the
auth with a token when replaying (``CachedTokenAuth.seed``).

vcrpy 8.3 and later patch httpx2's own transports (``httpx2.AsyncHTTPTransport``, which headgate's default network
transport is, and ``httpx2.MockTransport``), below headgate's whole stack: a cassette holds each send as it went on
the wire, once per attempt, and replays it there, so retries, the cache and the rate budget run over a replay as they
do over the network. A custom network transport (``headgate.curl.AsyncCurlTransport``, a
:class:`~headgate.testing.FakeTransport`) is not patched, and is neither recorded nor replayed. headgate's own tests
record and replay through real vcrpy. The filter itself depends on neither vcrpy nor pytest-recording, only on the
shape of their hooks.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Protocol

from headgate._auth import AUTH_HOP
from headgate._core import CredentialStamper
from headgate._url import sanitize_url

if TYPE_CHECKING:
    from collections.abc import Iterable, MutableMapping

    import httpx2

    from headgate._middleware import Next

__all__ = ["CassetteFilter", "RecordedRequest"]

#: The request headers that carry a credential whatever the auth: filtered from every recorded request.
_CREDENTIAL_HEADERS = ("authorization", "proxy-authorization", "cookie")

#: Whether the send in flight in this context is an auth flow's credential mint.
_MINTING: ContextVar[bool] = ContextVar("headgate_recording_auth_hop", default=False)


class RecordedRequest(Protocol):
    """The part of a vcrpy request :meth:`CassetteFilter.before_record_request` reads and rewrites."""

    uri: str


class CassetteFilter:
    """Filters for a vcrpy cassette that keep headgate's credentials and token mints out of it. See the module."""

    def __init__(
        self, *, auths: Iterable[object] = (), headers: Iterable[str] = (), params: Iterable[str] = ()
    ) -> None:
        """Filter the credentials *auths* declare, plus *headers* and query *params* named here.

        An auth that is a :class:`~headgate._core.CredentialStamper` contributes the headers and parameters it
        stamps; any other auth contributes nothing, so name its credentials in *headers* and *params*.
        """
        header_names = {name.lower() for name in (*_CREDENTIAL_HEADERS, *headers)}
        param_names = set(params)
        for auth in auths:
            if isinstance(auth, CredentialStamper):
                header_names.update(name.lower() for name in auth.credential_headers)
                param_names.update(auth.credential_params)
        self.filter_headers = tuple(sorted(header_names))
        self.filter_query_parameters = tuple(sorted(param_names))

    def vcr_config(self) -> dict[str, object]:
        """Return the vcrpy configuration: the header and query filters and the two record hooks."""
        return {
            "filter_headers": list(self.filter_headers),
            "filter_query_parameters": list(self.filter_query_parameters),
            "before_record_request": self.before_record_request,
            "before_record_response": self.before_record_response,
        }

    def before_record_request[R: RecordedRequest](self, request: R) -> R | None:
        """Return *request* with every secret query value and URL userinfo redacted, or ``None`` for a mint.

        vcrpy applies this hook to a request before matching it too, so a live request and its recording compare
        equal however its credential changes.
        """
        if _MINTING.get():
            return None
        request.uri = sanitize_url(request.uri)
        return request

    def before_record_response(self, response: MutableMapping[str, object]) -> MutableMapping[str, object]:
        """Return the recorded *response* without its ``Set-Cookie`` headers, which can carry a session."""
        headers = response.get("headers")
        if isinstance(headers, Mapping):
            response["headers"] = {name: value for name, value in headers.items() if str(name).lower() != "set-cookie"}
        return response

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Send *request*, marked for the recorder as a mint when an auth flow sent it to mint a credential."""
        if not request.extensions.get(AUTH_HOP):
            return await call_next(request)
        token = _MINTING.set(True)
        try:
            return await call_next(request)
        finally:
            _MINTING.reset(token)
