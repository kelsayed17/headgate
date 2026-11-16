# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every error attribute is read-only: what an error says about a call cannot be rewritten after it is raised."""

from __future__ import annotations

import copy
import pickle

import pytest

from headgate import AuthError, ClientError, RateLimitError, UpstreamStatusError
from headgate.errors import PageLimitError, Problem, RefusedError, ResponseParseError

#: The attributes every ``ClientError`` has.
_CALL = ("source", "method", "url", "request_id", "attempts", "retryable", "retry_after")

#: An instance of each error class, and the attributes it has beyond the ``ClientError`` ones.
_ERRORS: list[tuple[BaseException, tuple[str, ...]]] = [
    (ClientError("m", source="S", url="u", attempts=1, retryable=True, retry_after=1.0), _CALL),
    (
        UpstreamStatusError("m", status_code=503, code="c", problem=Problem()),
        (*_CALL, "status_code", "code", "problem"),
    ),
    (RateLimitError("m", status_code=429), (*_CALL, "status_code", "code", "problem")),
    (AuthError("m", status_code=401), (*_CALL, "status_code", "code", "problem")),
    (ResponseParseError("m", status_code=200, content_type="text/html"), (*_CALL, "status_code", "content_type")),
    (PageLimitError("m", pages=3), (*_CALL, "pages")),
    (RefusedError("m", code="c", kind=AuthError), ("message", "code", "kind")),
]


@pytest.mark.parametrize(
    ("error", "name"),
    [(error, name) for error, names in _ERRORS for name in names],
    ids=lambda value: value if isinstance(value, str) else type(value).__name__,
)
def test_an_error_attribute_cannot_be_assigned(error: BaseException, name: str) -> None:
    """Refuse to assign any attribute of any error, so a caller or a layer cannot rewrite what it says."""
    before = getattr(error, name)

    with pytest.raises(AttributeError):
        setattr(error, name, None)

    assert getattr(error, name) == before


@pytest.mark.parametrize(("error", "names"), _ERRORS, ids=lambda value: type(value).__name__)
def test_every_attribute_survives_a_copy_and_a_pickle(error: BaseException, names: tuple[str, ...]) -> None:
    """Keep every attribute through ``copy.deepcopy``, which replays the reduce protocol as ``pickle.loads`` does."""
    restored = copy.deepcopy(error)

    assert type(restored) is type(error)
    assert str(restored) == str(error)
    assert {name: getattr(restored, name) for name in names} == {name: getattr(error, name) for name in names}
    assert pickle.dumps(error)
