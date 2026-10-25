# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The ``ClientError`` tree: the distribution's public failure contract, including its shape and its pickling."""

from __future__ import annotations

import copy
import inspect
import pickle

import pytest

from headgate.errors import (
    AuthError,
    ClientError,
    ClientTimeoutError,
    RateLimitError,
    ResponseParseError,
    TransportError,
    UpstreamStatusError,
)

_UNAVAILABLE = 503
_OK = 200
_TOO_MANY = 429
_FOUND = 302


def test_hierarchy() -> None:
    """Pin the tree a boundary handler maps to responses."""
    assert issubclass(TransportError, ClientError)
    assert issubclass(ClientTimeoutError, TransportError)
    assert issubclass(UpstreamStatusError, ClientError)
    assert issubclass(RateLimitError, UpstreamStatusError)
    assert issubclass(AuthError, UpstreamStatusError)
    assert issubclass(ResponseParseError, ClientError)


def test_status_error_carries_status_code() -> None:
    """Carry the status and the source, and name the status in the message."""
    err = UpstreamStatusError("boom", status_code=_UNAVAILABLE, source="ExampleClient")
    assert err.status_code == _UNAVAILABLE
    assert err.source == "ExampleClient"
    assert str(err) == f"[ExampleClient] boom (status={_UNAVAILABLE})"


def test_every_status_error_carries_the_status_the_wire_sent() -> None:
    """Pin no status on any subclass: a quota refusal can arrive inside a 200 body."""
    assert RateLimitError("quota spent", status_code=_OK, source="X").status_code == _OK
    assert RateLimitError("slow down", status_code=_TOO_MANY, source="X").status_code == _TOO_MANY
    assert AuthError("key rejected", status_code=_FOUND, source="X").status_code == _FOUND


@pytest.mark.parametrize("cls", [UpstreamStatusError, RateLimitError, AuthError])
def test_the_status_is_required_on_every_subclass(cls: type[UpstreamStatusError]) -> None:
    """Require ``status_code`` as a keyword with no default, so no subclass can invent a status."""
    status_code = inspect.signature(cls).parameters["status_code"]
    assert status_code.kind is inspect.Parameter.KEYWORD_ONLY
    assert status_code.default is inspect.Parameter.empty


def test_a_status_error_carries_the_providers_own_code() -> None:
    """Name the provider's code beside the status, and leave it out when there is none."""
    err = RateLimitError("rate limit", status_code=_OK, code="50011", source="ExampleClient")
    assert err.code == "50011"
    assert "(status=200, code=50011)" in str(err)
    plain = UpstreamStatusError("boom", status_code=_UNAVAILABLE, source="X")
    assert plain.code is None
    assert "(status=503)" in str(plain)


def test_an_empty_code_is_not_the_same_as_no_code() -> None:
    """Name ``code=""`` in the message, since ``.code`` carries it, unlike ``code=None``."""
    err = UpstreamStatusError("boom", status_code=_OK, code="", source="X")
    assert err.code == ""
    assert "(status=200, code=)" in str(err)


def test_a_body_snippet_is_folded_into_the_message() -> None:
    """Append the body snippet, and append nothing for a missing or empty one."""
    assert str(UpstreamStatusError("boom", status_code=_OK, body="the reason")) == "boom (status=200) — the reason"
    assert str(UpstreamStatusError("boom", status_code=_OK, body="")) == "boom (status=200)"
    assert str(UpstreamStatusError("boom", status_code=_OK)) == "boom (status=200)"


@pytest.mark.parametrize("source", [None, ""])
def test_an_unnamed_source_adds_no_prefix(source: str | None) -> None:
    """Prefix the message only for a named source, so an empty name never renders as ``[]``."""
    err = ClientError("boom", source=source)
    assert str(err) == "boom"
    assert err.source == source


@pytest.mark.parametrize(
    "err",
    [
        ClientError("boom", source="X"),
        TransportError("net down", source="Y"),
        ClientTimeoutError("slow", source="Z"),
        UpstreamStatusError("order rejected: INVALID_SIZE_PRECISION", status_code=400, source="C"),
        RateLimitError("429", status_code=429, source="P"),
        AuthError("bad creds", status_code=403, code="FAILED", source="Q", body="denied"),
        ResponseParseError("truncated", source="R"),
    ],
)
def test_client_errors_survive_a_reduce_round_trip(err: ClientError) -> None:
    """Rebuild every ``ClientError`` exactly through its reduce protocol, as an unpickler does.

    ``copy.deepcopy`` replays ``__reduce_ex__`` (here :meth:`ClientError.__reduce__`) just as ``pickle.loads`` would,
    without deserializing bytes. ``pickle.dumps`` succeeding shows that the rebuild function is importable by name,
    which is the one extra thing unpickling needs.
    """
    err.add_note("context")
    restored = copy.deepcopy(err)
    assert type(restored) is type(err)
    assert str(restored) == str(err)
    assert restored.__dict__ == err.__dict__
    assert restored.__notes__ == ["context"]
    assert b"_rebuild_client_error" in pickle.dumps(err)
