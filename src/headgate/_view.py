# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The defaults a view made by :meth:`~headgate.AsyncBaseClient.with_options` lays over its client's calls.

A view is the same client with different defaults for its calls: extra headers, a retry policy and a deadline (its
timeouts replace the client's directly). :class:`ViewDefaults` holds them, and the client consults it where it applies
a call's own options, so the precedence is the call's, then the view's, then the route's, then the policy's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Final, Literal

import httpx2

from headgate._options import Timeouts
from headgate.retry import NO_RETRY

if TYPE_CHECKING:
    from collections.abc import Mapping

    from headgate.retry import RetryPolicy

__all__ = ["KEEP", "ROOT_DEFAULTS", "Keep", "ViewDefaults", "overlay_timeout"]


class Keep(Enum):
    """The ``with_options`` default that keeps a setting as it is, where ``None`` is itself a value."""

    KEEP = "keep"


#: Keep the setting the client already has.
KEEP: Final = Keep.KEEP


@dataclass(frozen=True, slots=True)
class ViewDefaults:
    """What a view adds to each of its calls: ``headers`` under the call's own, and a ``deadline`` and ``retry``.

    ``headers`` is a tuple of pairs so the value stays immutable; ``deadline`` is ``KEEP`` when the view sets none.
    """

    headers: tuple[tuple[str, str], ...] = ()
    deadline: float | Literal[Keep.KEEP] | None = KEEP
    retry: RetryPolicy | None = None

    def headers_for(self, call: Mapping[str, str] | None) -> Mapping[str, str] | httpx2.Headers | None:
        """Return the headers a call sends beyond the client's: the view's, then *call*'s, which win a clash."""
        if not self.headers:
            return call
        merged = httpx2.Headers(list(self.headers))
        if call is not None:
            merged.update(call)
        return merged

    def layered(
        self,
        *,
        headers: Mapping[str, str] | None,
        deadline: float | Literal[Keep.KEEP] | None,
        retry: bool | RetryPolicy | None,
    ) -> ViewDefaults:
        """Return these defaults with a further view's laid over them; ``None`` / ``KEEP`` keeps a setting.

        :raises ValueError: for a deadline that is not positive and finite.
        """
        changed = self
        if headers is not None:
            merged = httpx2.Headers(list(self.headers))
            merged.update(headers)
            changed = replace(changed, headers=tuple(merged.items()))
        if deadline is not KEEP:
            if deadline is not None and not (deadline > 0 and math.isfinite(deadline)):
                msg = f"deadline must be > 0 and finite, or None (got {deadline})"
                raise ValueError(msg)
            changed = replace(changed, deadline=deadline)
        if retry is not None:
            changed = replace(changed, retry=_retry_override(retry=retry))
        return changed


def _retry_override(*, retry: bool | RetryPolicy) -> RetryPolicy | None:
    """Return the retry policy a view's ``retry=`` installs: ``True`` is the client's own (no override)."""
    if retry is True:
        return None
    if retry is False:
        return NO_RETRY
    return retry


def overlay_timeout(base: Timeouts | float, view: Timeouts | float) -> Timeouts:
    """Return *base* with a view's timeout applied: a ``Timeouts`` replaces all four, a number the read timeout."""
    if isinstance(view, Timeouts):
        return view
    current = base if isinstance(base, Timeouts) else Timeouts(read=base)
    return replace(current, read=view)


#: The defaults of a client that is not a view: nothing added.
ROOT_DEFAULTS: Final = ViewDefaults()
