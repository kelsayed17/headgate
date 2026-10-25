""":class:`NoCloseStorage`: closing a client never closes a storage it shares.

One ``hishel.AsyncBaseStorage`` may be shared by many clients, each handed it as ``HttpCache(storage=...)``. Each
client's cache transport wraps it in a ``NoCloseStorage``, which delegates every method except ``close()``, so one
client's ``aclose()`` leaves the storage open for the others and for whoever created it.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from headgate._cache import NoCloseStorage


async def test_no_close_storage_delegates_but_skips_close() -> None:
    inner = AsyncMock(name="inner_storage")
    wrapper = NoCloseStorage(inner)

    entry_id = uuid.uuid4()
    req = MagicMock(name="request")
    resp = MagicMock(name="response")
    new_entry = MagicMock(name="new_entry")

    await wrapper.get_entries("key-1")
    inner.get_entries.assert_awaited_once_with("key-1")

    await wrapper.create_entry(req, resp, "key-2")
    inner.create_entry.assert_awaited_once_with(req, resp, "key-2", None)

    await wrapper.update_entry(entry_id, new_entry)
    inner.update_entry.assert_awaited_once_with(entry_id, new_entry)

    await wrapper.remove_entry(entry_id)
    inner.remove_entry.assert_awaited_once_with(entry_id)

    await wrapper.close()
    inner.close.assert_not_awaited()
