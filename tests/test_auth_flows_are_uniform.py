"""Every ``httpx2.Auth`` in the package runs one kind of flow, and declares nothing httpx2 will ignore.

Two rules:

* **Every auth overrides ``async_auth_flow``.** A sync ``auth_flow`` is the one shape that would let a caller's headers
  be overwritten, and this keeps a new auth from arriving that way.
* **No auth declares ``requires_request_body`` / ``requires_response_body``.** httpx2 reads those only inside its
  DEFAULT flows, so on a class that overrides ``async_auth_flow`` they are inert. A flow that needs the body reads it
  itself (``await request.aread()``).

The auth classes are found by walking ``httpx2.Auth``'s subclasses defined under ``headgate``.
"""

from __future__ import annotations

import importlib

import httpx2
import pytest

importlib.import_module("headgate._auth")


def _auths() -> list[type[httpx2.Auth]]:
    found: list[type[httpx2.Auth]] = []
    pending = list(httpx2.Auth.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith("headgate."):
            found.append(cls)
    return sorted(set(found), key=lambda c: c.__qualname__)


_AUTHS = _auths()


def test_the_walk_finds_the_auths() -> None:
    """Guard the guard: an empty walk would pass every test below."""
    names = {cls.__name__ for cls in _AUTHS}
    assert {"StaticHeaderAuth", "QueryParamAuth", "CachedTokenAuth"} <= names


@pytest.mark.parametrize("auth", _AUTHS, ids=lambda c: c.__name__)
def test_the_flow_is_async(auth: type[httpx2.Auth]) -> None:
    assert auth.async_auth_flow is not httpx2.Auth.async_auth_flow
    assert auth.auth_flow is httpx2.Auth.auth_flow, "a sync auth_flow would be the only one here"


@pytest.mark.parametrize("auth", _AUTHS, ids=lambda c: c.__name__)
def test_no_inert_body_flags(auth: type[httpx2.Auth]) -> None:
    for flag in ("requires_request_body", "requires_response_body"):
        assert flag not in vars(auth), f"{flag} is ignored once async_auth_flow is overridden — read the body instead"
