# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The defaults a view made by :meth:`~headgate.Client.with_options` lays over its client's calls.

A view is the same client with different defaults for its calls: extra headers, a retry policy, a deadline and a
redirect choice (its timeouts lie over the client's directly, through ``Timeouts.over``). :class:`ViewDefaults` holds
them, and the client consults it where it applies a call's own options, so the precedence is the call's, then the
view's, then the route's, then the policy's.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Final, Literal

import httpx2

# A closed TypedDict (PEP 728) refuses a keyword it does not name, so a stray or misspelled option is a type error.
from typing_extensions import TypedDict

from headgate._call_options import check_overrides
from headgate._options import check_deadline
from headgate.retry._policy import SEND_ONCE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from headgate._options import Timeouts
    from headgate.retry import RetryPolicy

__all__ = ["KEEP", "ROOT_DEFAULTS", "VIEW_KEYS", "Keep", "ViewDefaults", "ViewOptions"]


class ViewOptions(TypedDict, total=False, closed=True):
    """The defaults :meth:`~headgate.Client.with_options` changes; an option left out keeps the outer one.

    * ``headers``: sent on every call of the view, over the client's; a call's ``headers=`` still wins a clash.
    * ``timeout``: :class:`~headgate.Timeouts` whose set phases replace the client's; the others are kept.
    * ``deadline``: seconds each call may take, or ``None`` for no deadline.
    * ``retry``: a :class:`~headgate.RetryPolicy` that replaces the client's and its routes', or ``None`` to send each
      call once. A call that should not take the view's is made on the client.
    * ``follow_redirects``: ``False`` makes a redirect raise as its 3xx; ``True`` follows it.
    """

    headers: Mapping[str, str]
    timeout: Timeouts
    deadline: float | None
    retry: RetryPolicy | None
    follow_redirects: bool


#: The keywords :class:`ViewOptions` names, read once.
VIEW_KEYS: Final = frozenset(ViewOptions.__optional_keys__)


class Keep(Enum):
    """A view's setting when it sets none, where ``None`` is itself a value (no deadline, or send once)."""

    KEEP = "keep"


#: Keep the setting the client already has.
KEEP: Final = Keep.KEEP


@dataclass(frozen=True, slots=True)
class ViewDefaults:
    """What a view adds to each of its calls: ``headers`` under the call's own, a ``deadline``, ``retry`` and redirects.

    ``headers`` is a tuple of pairs so the value stays immutable; every other setting is ``KEEP`` when the view sets
    none. A view's ``retry=None`` is held as the policy that sends once.
    """

    headers: tuple[tuple[str, str], ...] = ()
    deadline: float | Literal[Keep.KEEP] | None = KEEP
    retry: RetryPolicy | Literal[Keep.KEEP] = KEEP
    follow_redirects: bool | Literal[Keep.KEEP] = KEEP

    def headers_for(self, call: Mapping[str, str] | None) -> Mapping[str, str] | httpx2.Headers | None:
        """Return the headers a call sends beyond the client's: the view's, then *call*'s, which win a clash."""
        if not self.headers:
            return call
        merged = httpx2.Headers(list(self.headers))
        if call is not None:
            merged.update(call)
        return merged

    def layered(self, options: ViewOptions) -> ViewDefaults:
        """Return these defaults with a further view's *options* laid over them; an option left out keeps a setting.

        :raises TypeError: for a ``retry`` or ``follow_redirects`` of the wrong kind (see
            :func:`~headgate._call_options.check_overrides`).
        :raises ValueError: for a deadline that is not positive and finite.
        """
        check_overrides(options)
        changed = self
        if (headers := options.get("headers")) is not None:
            merged = httpx2.Headers(list(self.headers))
            merged.update(headers)
            changed = replace(changed, headers=tuple(merged.items()))
        if "deadline" in options:
            deadline = options["deadline"]
            check_deadline(deadline)
            changed = replace(changed, deadline=deadline)
        if "retry" in options:
            retry = options["retry"]
            changed = replace(changed, retry=SEND_ONCE if retry is None else retry)
        if "follow_redirects" in options:
            changed = replace(changed, follow_redirects=options["follow_redirects"])
        return changed


#: The defaults of a client that is not a view: nothing added.
ROOT_DEFAULTS: Final = ViewDefaults()
