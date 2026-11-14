# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""What a walk remembers to catch a loop: a fixed-size digest per page, and at most so many of them."""

from __future__ import annotations

from headgate.pagination._paginated import REMEMBERED_PAGES, Remembered


def test_a_key_is_remembered_once() -> None:
    """Report a key seen before, and remember a new one."""
    remembered = Remembered()

    assert remembered.add(b"a" * 10_000)
    assert not remembered.add(b"a" * 10_000)


def test_only_the_most_recent_keys_are_remembered() -> None:
    """Forget the oldest key once more than :data:`REMEMBERED_PAGES` are held, so a long walk holds a bounded set."""
    remembered = Remembered()
    for number in range(REMEMBERED_PAGES + 1):
        assert remembered.add(str(number).encode())

    assert len(remembered) == REMEMBERED_PAGES
    assert remembered.add(b"0"), "the oldest key was forgotten"
    assert not remembered.add(str(REMEMBERED_PAGES).encode())
