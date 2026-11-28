# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Build a value on a given timing, as every object built inside ``timing_override(timing)`` is."""

from __future__ import annotations

from typing import TYPE_CHECKING

from headgate.timing import timing_override

if TYPE_CHECKING:
    from collections.abc import Callable

    from headgate.timing import Timing


def built_on[T](timing: Timing, build: Callable[[], T]) -> T:
    """Return what *build* builds while *timing* is the current timing, which it reads when it is built."""
    with timing_override(timing):
        return build()
