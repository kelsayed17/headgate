# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Every exported class and function reports its public home: the module a user imports it from.

A class defined in a private module would otherwise name that module in its ``repr``, in a traceback and in
``help()``, which is a second home no import may use. Each public module calls :func:`rehome` last, which also gives it
the ``__getattr__`` that names the home of a name asked of the wrong public module.
"""

import dis
import inspect
import sys
import types
import typing
from typing import TYPE_CHECKING, Final, NoReturn, cast

from headgate._core._homes import HOMES

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

__all__ = ["rehome"]


def rehome(module: str, namespace: MutableMapping[str, object]) -> None:
    """Set ``__module__`` to *module* on every class and function the ``__all__`` of *namespace* names.

    A type alias (``type Body = ...``) and a value such as a reader are neither, so they keep theirs: a
    ``TypeAliasType`` refuses a new ``__module__``.

    A class also loses its ``__firstlineno__``, the line of the private module that defines it, which ``inspect`` would
    otherwise read in the public module's file: ``inspect.getsource`` then raises ``OSError`` for every rehomed class,
    rather than return another object's lines. Setting ``__module__`` drops it from a plain class already, but not
    from a ``Protocol`` or a ``TypedDict``.

    The module's ``__getattr__`` is set here too (:func:`_misplaced`), not written in the module, so a type checker,
    which reads a module ``__getattr__`` as one that has every name, never sees it.
    """
    for name in cast("list[str]", namespace["__all__"]):
        value = namespace[name]
        if isinstance(value, type | types.FunctionType):
            # typing files overloads under their function's module, so they move with it.
            overloads = typing.get_overloads(value) if isinstance(value, types.FunctionType) else []
            value.__module__ = module
            for form in overloads:
                form.__module__ = module
                typing.overload(form)
            if isinstance(value, type) and "__firstlineno__" in vars(value):
                delattr(value, "__firstlineno__")
    namespace["__getattr__"] = _misplaced(module)


def _misplaced(module: str) -> Callable[[str], NoReturn]:
    """Return the ``__getattr__`` of the public module *module*, called for a name it does not bind.

    A name another public module exports (:data:`HOMES`) is refused naming its home, ``RetryPolicy lives in headgate:
    from headgate import RetryPolicy``. Under ``from module import name`` the refusal is an ``ImportError``, since the
    import statement drops an ``AttributeError``'s message for its own bare one; everywhere else it is the
    ``AttributeError`` a missing attribute raises, so ``hasattr`` and ``getattr`` with a default answer as ever. Any
    other name raises the usual ``AttributeError``.
    """

    def missing(name: str) -> NoReturn:
        homes = () if name.startswith("_") else HOMES.get(name, ())
        if not homes:
            msg = f"module {module!r} has no attribute {name!r}"
            raise AttributeError(msg, name=name, obj=sys.modules.get(module))
        ways = ", or ".join(f"from {home} import {name}" for home in homes)
        msg = f"{name} lives in {' and '.join(homes)}: {ways}"
        here = inspect.currentframe()
        if here is not None and here.f_back is not None and _importing(here.f_back):
            error = ImportError(msg, name=module)
            error.name_from = name  # as the import system's own ImportError for ``from module import name``
            raise error
        raise AttributeError(msg, name=name, obj=sys.modules.get(module))

    return missing


#: The instruction a ``from module import name`` statement asks the module for the name with.
_IMPORT_FROM: Final = dis.opmap["IMPORT_FROM"]


def _importing(frame: types.FrameType) -> bool:
    """Return whether *frame* is running the ``IMPORT_FROM`` of a ``from module import name`` statement."""
    code = frame.f_code.co_code
    return 0 <= frame.f_lasti < len(code) and code[frame.f_lasti] == _IMPORT_FROM
