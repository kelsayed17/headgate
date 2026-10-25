# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The ``ClientError`` tree: this distribution's public failure contract.

``headgate.errors`` is imported by nearly every module, so its pickling behaviour and its subclass shape are a
published contract.
"""

from __future__ import annotations

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


def test_hierarchy() -> None:
    assert issubclass(TransportError, ClientError)
    assert issubclass(ClientTimeoutError, TransportError)
    assert issubclass(UpstreamStatusError, ClientError)
    assert issubclass(RateLimitError, UpstreamStatusError)
    assert issubclass(AuthError, UpstreamStatusError)
    assert issubclass(ResponseParseError, ClientError)


def test_status_error_carries_status_code() -> None:
    err = UpstreamStatusError("boom", status_code=503, source="ExampleClient")
    assert err.status_code == 503
    assert err.source == "ExampleClient"
    assert "503" in str(err)


def test_every_status_error_carries_the_status_the_wire_sent() -> None:
    """A quota refusal can arrive inside a 200 body, so no subclass pins or defaults a status."""
    assert RateLimitError("quota spent", status_code=200, source="X").status_code == 200
    assert RateLimitError("slow down", status_code=429, source="X").status_code == 429
    assert AuthError("key rejected", status_code=302, source="X").status_code == 302


@pytest.mark.parametrize("cls", [UpstreamStatusError, RateLimitError, AuthError])
def test_the_status_is_required_on_every_subclass(cls: type[UpstreamStatusError]) -> None:
    with pytest.raises(TypeError):
        cls("no status", source="X")  # ty: ignore[missing-argument]  # the TypeError IS the test


def test_a_status_error_carries_the_providers_own_code() -> None:
    err = RateLimitError("rate limit", status_code=200, code="50011", source="ExampleClient")
    assert err.code == "50011"
    assert "(status=200, code=50011)" in str(err)
    plain = UpstreamStatusError("boom", status_code=503, source="X")
    assert plain.code is None
    assert "(status=503)" in str(plain)


def test_an_empty_code_is_not_the_same_as_no_code() -> None:
    """``code=""`` is a real (if empty) code, so the message still names it — unlike ``code=None``.

    A truthiness check (``if code`` instead of ``if code is not None``) would drop ``code=`` from the message while
    ``.code`` still carries ``""``.
    """
    err = UpstreamStatusError("boom", status_code=200, code="", source="X")
    assert err.code == ""
    assert "(status=200, code=)" in str(err)


def test_the_code_survives_a_pickle_round_trip() -> None:
    err = UpstreamStatusError("rejected", status_code=200, code="FAILED", source="ExampleClient")
    restored = pickle.loads(pickle.dumps(err))
    assert (restored.status_code, restored.code, str(restored)) == (200, "FAILED", str(err))


@pytest.mark.parametrize(
    "err",
    [
        ClientError("boom", source="X"),
        TransportError("net down", source="Y"),
        ClientTimeoutError("slow", source="Z"),
        UpstreamStatusError("order rejected: INVALID_SIZE_PRECISION", status_code=400, source="C"),
        RateLimitError("429", status_code=429, source="P"),
        AuthError("bad creds", status_code=403, source="Q"),
        ResponseParseError("truncated", source="R"),
    ],
)
def test_client_errors_survive_pickle_round_trip(err: ClientError) -> None:
    """An exception crossing a process or durable-workflow boundary is pickled, and must unpickle.

    The subclasses take required keyword-only ``__init__`` arguments, which the default ``(cls, self.args)`` reduce
    drops, so unpickling would raise ``TypeError``. ``__reduce__`` round-trips type, message, source and status_code.
    """
    restored = pickle.loads(pickle.dumps(err))
    assert type(restored) is type(err)
    assert str(restored) == str(err)
    assert restored.source == err.source
    assert getattr(restored, "status_code", None) == getattr(err, "status_code", None)
