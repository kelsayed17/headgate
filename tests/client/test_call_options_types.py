# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The type checker lets a wrapper typed on the call options forward them, and refuses the removed override values."""

from __future__ import annotations

import asyncio
import sys
from textwrap import dedent, indent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_PRELUDE = dedent(
    """
    from typing import LiteralString, Unpack

    from headgate import CallOptions, Client, Paginated, RequestOptions, nothing
    from headgate.pagination import NextUrl

    PAGES = NextUrl[None, object](items=lambda _page: (), next=lambda _page: None)


    async def probe(c: Client, url: LiteralString) -> None:
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


async def test_a_wrapper_forwards_the_call_options(tmp_path: Path) -> None:
    """Forward ``**options: Unpack[CallOptions]`` to a verb and to ``paginate``."""
    code, out = await _check(
        tmp_path,
        """
        async def fetch(**options: Unpack[CallOptions]) -> None:
            await c.get(url, expect=nothing, **options)
            await c.post(url, expect=nothing, **options)
            c.paginate(url, expect=nothing, pages=PAGES, **options)

        await fetch(retry=None, follow_redirects=False, cache_ttl=None)
        """,
    )

    assert code == 0, out


async def test_stream_options_leave_out_what_a_stream_never_buffers(tmp_path: Path) -> None:
    """Refuse forwarding a verb's options to ``stream``, which takes no ``cache_ttl`` or ``max_response_bytes``."""
    code, out = await _check(
        tmp_path,
        """
        async def opened(**options: Unpack[RequestOptions]) -> None:
            async with c.stream(url, **options):
                pass
        """,
    )

    assert code != 0, out
    assert out.count("error[unknown-argument]") == len(("cache_ttl", "max_response_bytes")), out
    assert "Argument `cache_ttl` does not match" in out, out
    assert "Argument `max_response_bytes` does not match" in out, out


async def test_the_old_override_values_do_not_type_check(tmp_path: Path) -> None:
    """Refuse ``retry=True``, ``retry=False`` and ``follow_redirects=None`` on a call and on a view."""
    code, out = await _check(
        tmp_path,
        """
        await c.get(url, expect=nothing, retry=True)
        await c.get(url, expect=nothing, retry=False)
        await c.get(url, expect=nothing, follow_redirects=None)
        c.with_options(retry=False)
        """,
    )

    assert code != 0, out
    assert out.count("error[invalid-argument-type]") == len(("True", "False", "None", "the view's False")), out


async def test_a_walk_names_its_items_first_and_its_page_type_only_when_it_matters(tmp_path: Path) -> None:
    """Accept ``Paginated[object]`` and ``Paginated[object, None]`` for a walk, and refuse the page type first."""
    code, out = await _check(
        tmp_path,
        """
        items: Paginated[object] = c.paginate(url, expect=nothing, pages=PAGES)
        both: Paginated[object, None] = c.paginate(url, expect=nothing, pages=PAGES, max_pages=2, on_limit="stop")
        flipped: Paginated[None, object] = c.paginate(url, expect=nothing, pages=PAGES)
        """,
    )

    assert code != 0, out
    assert out.count("error[") == 1, out
    assert "flipped" in out, out
