# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every ``httpx2.Auth`` in the package runs one kind of flow, and declares nothing httpx2 would ignore.

Two rules:

* **Every auth overrides ``async_auth_flow``.** A sync ``auth_flow`` is the one shape that would let a caller's headers
  be overwritten, and this keeps a new auth from arriving that way.
* **No auth declares ``requires_request_body`` / ``requires_response_body``.** httpx2 reads those only inside its
  default flows, so on a class that overrides ``async_auth_flow`` they are inert. A flow that needs the body reads it
  itself (``await request.aread()``).

The auth classes are found by walking ``httpx2.Auth``'s subclasses defined under ``headgate``.
"""

from __future__ import annotations

import httpx2
import pytest

from headgate._auth import CachedTokenAuth, QueryParamAuth, StaticHeaderAuth


def _auths() -> list[type[httpx2.Auth]]:
    found: set[type[httpx2.Auth]] = set()
    pending = list(httpx2.Auth.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith("headgate."):
            found.add(cls)
    return sorted(found, key=lambda c: c.__qualname__)


_AUTHS = _auths()


def test_the_walk_finds_the_auths() -> None:
    """Guard the guard: an empty walk would pass every test below."""
    assert {StaticHeaderAuth, QueryParamAuth, CachedTokenAuth} <= set(_AUTHS)


@pytest.mark.parametrize("auth", _AUTHS, ids=lambda c: c.__name__)
def test_the_flow_is_async(auth: type[httpx2.Auth]) -> None:
    """Override ``async_auth_flow`` and leave the sync ``auth_flow`` as httpx2's."""
    assert auth.async_auth_flow is not httpx2.Auth.async_auth_flow
    assert auth.auth_flow is httpx2.Auth.auth_flow, "a sync auth_flow would be the only one here"


@pytest.mark.parametrize("auth", _AUTHS, ids=lambda c: c.__name__)
def test_no_inert_body_flags(auth: type[httpx2.Auth]) -> None:
    """Declare no body flag that the overridden flow would ignore."""
    for flag in ("requires_request_body", "requires_response_body"):
        assert flag not in vars(auth), f"{flag} is ignored once async_auth_flow is overridden: read the body instead"
