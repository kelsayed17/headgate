"""Codec-neutral helpers for preparing request bodies: shared by every flavor's ``to_builtins``."""

from __future__ import annotations

__all__ = ["drop_none"]


def drop_none(obj: object) -> object:
    """Recursively drop ``None``-valued keys, walking nested dicts and lists.

    Every other value survives, so ``False``, ``0`` and ``""`` stay. That separates this from an ``omit_defaults``
    setting, and makes it the analog of pydantic's ``exclude_none``. Insertion order is preserved.
    """
    if isinstance(obj, dict):
        return {k: drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [drop_none(v) for v in obj]
    return obj
