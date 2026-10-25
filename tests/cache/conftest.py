"""Fixtures for the cache suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anysqlite
import pytest
from hishel import AsyncSqliteStorage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def storage() -> AsyncIterator[AsyncSqliteStorage]:
    """A real hishel storage on an in-memory SQLite database, closed after the test."""
    conn = await anysqlite.connect(":memory:")
    store = AsyncSqliteStorage(connection=conn)
    try:
        yield store
    finally:
        await store.close()
