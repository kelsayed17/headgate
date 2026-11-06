# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The type checker refuses a computed ``str`` as a call's URL: it skipped escaping."""

from __future__ import annotations

import asyncio
import sys
from textwrap import dedent, indent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_PRELUDE = dedent(
    """
    import httpx2

    from headgate import nothing
    from headgate.pagination import PageRequest
    from tests.support.clients import StubClient


    async def probe(c: StubClient, uid: str, next_url: str) -> None:
    """
)


async def _check(path: Path, body: str) -> tuple[int | None, str]:
    """Type check a module whose ``probe`` runs *body*, and return the checker's exit code and its output."""
    module = path / "probe.py"
    module.write_text(_PRELUDE + indent(dedent(body), "    "))
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ty", "check", str(module), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode() + stderr.decode()


async def test_a_computed_string_url_does_not_type_check(tmp_path: Path) -> None:
    """Refuse an f-string URL to every verb, a paginated walk and a page request, naming the type a URL must have."""
    code, out = await _check(
        tmp_path,
        """
        await c.get(f"items/{uid}", expect=nothing)
        async with c.stream(f"items/{uid}"):
            pass
        c.paginate(f"items/{uid}", expect=nothing, pages=nothing)
        PageRequest(f"items/{uid}")
        """,
    )

    assert code != 0, out
    assert "LiteralString" in out, out
    assert out.count("Expected `LiteralString | Template | URL`, found `str`") == len(
        ("get", "stream", "paginate", "page")
    ), out


async def test_a_literal_a_t_string_and_a_url_object_type_check(tmp_path: Path) -> None:
    """Accept a literal, a t-string and an ``httpx2.URL``."""
    code, out = await _check(
        tmp_path,
        """
        await c.get("items/1", expect=nothing)
        await c.get(t"items/{uid}", expect=nothing)
        await c.get(httpx2.URL(next_url), expect=nothing)
        """,
    )

    assert code == 0, out
