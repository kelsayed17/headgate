# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Codec-neutral request-body preparation shared by every flavor's ``to_builtins``."""

from __future__ import annotations

__all__ = ["drop_none"]


def drop_none(obj: object) -> object:
    """Return *obj* with every ``None``-valued key removed, walking nested dicts, lists and tuples.

    Every other value survives, so ``False``, ``0`` and ``""`` stay (unlike msgspec's ``omit_defaults``; like
    pydantic's ``exclude_none``). Insertion order is preserved. Tuples are walked because ``msgspec.to_builtins`` keeps
    a ``tuple`` field as a tuple, and the structs inside one have ``None`` fields like any other.
    """
    if isinstance(obj, dict):
        return {k: drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [drop_none(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(drop_none(v) for v in obj)
    return obj
