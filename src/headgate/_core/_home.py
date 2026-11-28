# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every exported class and function reports its public home: the module a user imports it from.

A class defined in a private module would otherwise name that module in its ``repr``, in a traceback and in
``help()``, which is a second home no import may use. Each public module calls :func:`rehome` last.
"""

import types
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["rehome"]


def rehome(module: str, namespace: Mapping[str, object]) -> None:
    """Set ``__module__`` to *module* on every class and function the ``__all__`` of *namespace* names.

    A type alias (``type Body = ...``) and a value such as a reader are neither, so they keep theirs: a
    ``TypeAliasType`` refuses a new ``__module__``.
    """
    for name in cast("list[str]", namespace["__all__"]):
        value = namespace[name]
        if isinstance(value, type | types.FunctionType):
            value.__module__ = module
