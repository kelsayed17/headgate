# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""One rule for finality: every exported class is final, or an extension base the docs say a user may subclass.

A final class is final to the type checker (``typing.final``, which ty enforces) and at runtime: defining a subclass
raises ``TypeError`` naming the class and saying what to write instead. headgate reads such a class by its fields, or
holds it as a handle, so an override would be accepted and never run. An extension base is a class headgate calls
through its methods, so a subclass's override is what runs, and the guide on it says a user may subclass it.

A protocol and a ``TypedDict`` are shapes, not classes to finalize; an enum with members is final by construction.
"""

from __future__ import annotations

import enum
import importlib
import types
from typing import TYPE_CHECKING, Any, Final, cast

import pytest

# typing's is_typeddict misses a typing_extensions TypedDict, which a closed one (PEP 728) is before 3.15.
from typing_extensions import is_typeddict

from tests.package.test_api_surface import _PUBLIC_MODULES

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

#: The exported classes a user may subclass, each with where the docs say so.
_EXTENSION_BASES: Final[Mapping[str, str]] = {
    "headgate.Auth": "auth.md, your own auth: an auth of your own subclasses headgate.Auth",
    "headgate.SigningAuth": "auth.md, signing auth: a subclass supplies sign(request, now)",
    "headgate.CachedTokenAuth": "auth.md, cached token auth: a subclass supplies mint(request)",
    "headgate.StaticHeaderAuth": "auth.md, your own auth: every auth may be subclassed, as BearerAuth subclasses it",
    "headgate.BearerAuth": "auth.md, your own auth: every auth may be subclassed",
    "headgate.QueryParamAuth": "auth.md, your own auth: every auth may be subclassed",
    "headgate.pagination.WritingPager": "pagination.md, a pager of your own: one that writes bodies subclasses it",
    "headgate.pagination.Cursor": "pagination.md, a pager of your own: a strategy's subclass may override its hooks",
    "headgate.pagination.Offset": "pagination.md, a pager of your own: as Cursor",
    "headgate.pagination.PageNumber": "pagination.md, a pager of your own: as Cursor",
    "headgate.pagination.LinkHeader": "pagination.md, a pager of your own: as Cursor",
    "headgate.pagination.NextUrl": "pagination.md, a pager of your own: as Cursor",
    "headgate.pagination.BodyCursor": "pagination.md, a pager of your own: as Cursor",
    "headgate.pagination.BodyOffset": "pagination.md, a pager of your own: as Cursor",
    "headgate.msgspec.MsgspecCodec": "codecs.md, a codec of your own: it may subclass a flavor's codec",
    "headgate.pydantic.PydanticCodec": "codecs.md, a codec of your own: as MsgspecCodec",
    "headgate.events.CallEvent": "observability.md: the base an observer matches every call's event on, which "
    "headgate's own events subclass, so it cannot be final",
    "headgate.ClientError": "errors.md: an error may be subclassed to name a failure of your own",
    "headgate.TransportError": "errors.md: as ClientError",
    "headgate.ClientTimeoutError": "errors.md: as ClientError",
    "headgate.UpstreamStatusError": "errors.md: as ClientError; RefusedError(kind=) raises a refusal as one",
    "headgate.AuthError": "errors.md: as ClientError",
    "headgate.RateLimitError": "errors.md: as ClientError",
    "headgate.errors.CircuitOpenError": "errors.md: as ClientError",
    "headgate.errors.ClientConfigurationError": "errors.md: as ClientError",
    "headgate.errors.ForbiddenDestinationError": "errors.md: as ClientError",
    "headgate.errors.PageLimitError": "errors.md: as ClientError",
    "headgate.errors.ResponseParseError": "errors.md: as ClientError",
    "headgate.errors.ResponseTooLargeError": "errors.md: as ClientError",
}


def _classes() -> Iterator[tuple[str, type]]:
    """Yield each class a public module exports, with its dotted name, but the protocols and ``TypedDict`` s."""
    for module_name in _PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        for name in module.__all__:
            export = getattr(module, name)
            if isinstance(export, type) and not (getattr(export, "_is_protocol", False) or is_typeddict(export)):
                yield f"{module_name}.{name}", export


def _subclass(base: type) -> type:
    """Define a subclass of *base*, as a class statement does; typed as any base, which a checker cannot see through."""
    return types.new_class("_Sub", (cast("Any", base),))


_CLASSES: Final = dict(_classes())
_FINAL: Final = [name for name, cls in _CLASSES.items() if name not in _EXTENSION_BASES]


@pytest.mark.parametrize("name", _FINAL)
def test_a_class_that_is_no_extension_base_is_final(name: str) -> None:
    """Refuse a subclass where it is defined, saying what to write instead, as ``typing.final`` tells ty to."""
    cls = _CLASSES[name]
    if issubclass(cls, enum.Enum):
        assert list(cls), f"{name} is an enum without members, which a subclass could extend"
        with pytest.raises(TypeError):
            _subclass(cls)
        return
    assert getattr(cls, "__final__", False), f"{name} is not typing.final"
    with pytest.raises(TypeError, match=rf"^_Sub cannot subclass {cls.__name__}\b.*: \S"):
        _subclass(cls)


@pytest.mark.parametrize("name", sorted(_EXTENSION_BASES))
def test_an_extension_base_is_open(name: str) -> None:
    """Leave an extension base open to the type checker, so a subclass the docs describe type-checks."""
    assert name in _CLASSES, f"{name} is not an exported class"
    assert not getattr(_CLASSES[name], "__final__", False), f"{name} is typing.final"


def test_both_kinds_are_found() -> None:
    """Find final classes and extension bases both, so an emptied walk cannot pass the checks above unseen."""
    assert "headgate.Client" in _FINAL
    assert "headgate.Timeouts" in _FINAL
    assert "headgate.Auth" in _CLASSES
