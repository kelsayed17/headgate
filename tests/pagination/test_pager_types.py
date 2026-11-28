# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A pager carries its page reader, so ty checks a lambda against the page type and infers the walk's items."""

from __future__ import annotations

import asyncio
import sys
from textwrap import dedent, indent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_PRELUDE = dedent(
    """
    from typing import reveal_type

    import msgspec

    from headgate import Client
    from headgate.msgspec import reader
    from headgate.pagination import Cursor, LinkHeader


    class Issue(msgspec.Struct, frozen=True):
        number: int


    class Page(msgspec.Struct, frozen=True):
        items: list[Issue]
        next: str | None = None


    ISSUE_PAGES = LinkHeader(reader(list[Issue]), items=lambda page: page)


    async def probe(c: Client) -> None:
    """
)


async def _check(path: Path, body: str) -> tuple[int | None, str]:
    """Type check a module whose ``probe`` runs *body*, and return the checker's exit code and its output."""
    module = path / "probe.py"
    module.write_text(_PRELUDE + indent(dedent(body), "    "))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ty",
        "check",
        "--output-format",
        "concise",
        str(module),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode() + stderr.decode()


async def test_a_lambda_pager_binds_its_page_and_item_types(tmp_path: Path) -> None:
    """Infer the docs' ``LinkHeader(reader(list[Issue]), items=lambda page: page)`` and its walk, item first."""
    code, out = await _check(
        tmp_path,
        """
        reveal_type(ISSUE_PAGES)
        reveal_type(c.paginate("repos/o/r/issues", pager=ISSUE_PAGES))
        async for issue in c.paginate("repos/o/r/issues", pager=ISSUE_PAGES):
            reveal_type(issue)
        """,
    )

    assert code == 0, out
    assert "Revealed type: `LinkHeader[Issue, list[Issue]]`" in out, out
    assert "Revealed type: `Paginated[Issue, list[Issue]]`" in out, out
    assert "Revealed type: `Issue`" in out, out


async def test_a_typo_in_a_lambda_pager_is_a_type_error(tmp_path: Path) -> None:
    """Report a lambda that reads an attribute the page type does not have, on the pager and on an item."""
    code, out = await _check(
        tmp_path,
        """
        Cursor(reader(Page), items=lambda p: p.itemz, next=lambda p: p.next, param="cursor")
        async for issue in c.paginate("issues", pager=ISSUE_PAGES):
            issue.ident
        """,
    )

    assert code != 0, out
    assert out.count("unresolved-attribute") == len(("itemz", "ident")), out


async def test_paginate_takes_no_expect_and_no_pages(tmp_path: Path) -> None:
    """Refuse ``expect=``, which the pager carries now, and ``pages=``, which is ``pager=``."""
    code, out = await _check(
        tmp_path,
        """
        c.paginate("issues", expect=reader(list[Issue]), pager=ISSUE_PAGES)
        c.paginate("issues", pages=ISSUE_PAGES)
        """,
    )

    assert code != 0, out
    assert out.count("no-matching-overload") == len(("expect", "pages")), out
