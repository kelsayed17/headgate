# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The type checker refuses a non-reader ``expect=``, and a reader widens through ``Expect.on``."""

from __future__ import annotations

import asyncio
import sys
from textwrap import dedent, indent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_PRELUDE = dedent(
    """
    import msgspec

    from headgate import Expect, Reader, as_bytes, nothing
    from headgate.msgspec import decoder
    from tests.support.clients import StubClient


    class Order(msgspec.Struct):
        id: int


    ORDER = decoder(Order)


    async def probe(c: StubClient) -> None:
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


async def test_a_non_reader_expect_does_not_type_check(tmp_path: Path) -> None:
    """Refuse a builtin class, a model class and a bare lambda as ``expect=``, and a bare lambda as ``Expect``'s."""
    code, out = await _check(
        tmp_path,
        """
        await c.get("x", expect=str)
        await c.get("x", expect=Order)
        await c.get("x", expect=lambda r: r)
        Expect(lambda r: r)
        """,
    )

    assert code != 0, out
    assert out.count("invalid-argument-type") == len(("str", "Order", "lambda", "Expect")), out


async def test_stream_takes_no_expect(tmp_path: Path) -> None:
    """Refuse ``expect=`` on a stream, which has no reader."""
    code, out = await _check(
        tmp_path,
        """
        c.stream("x", expect=as_bytes)
        """,
    )

    assert code != 0, out
    assert out.count("unknown-argument") == 1, out


async def test_a_reader_widens_through_expect_on(tmp_path: Path) -> None:
    """Accept a reader where a reader of a wider type goes, and an ``Expect`` whose ``on`` widened its result."""
    code, out = await _check(
        tmp_path,
        """
        e: Expect[Order | None] = Expect(ORDER).on(404, nothing)
        r: Reader[Order | None] = ORDER
        del e, r
        """,
    )

    assert code == 0, out
