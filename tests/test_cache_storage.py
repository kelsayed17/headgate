"""The no-close storage wrapper + the composition-owned storage wiring.

The ONE process-shared ``hishel.AsyncBaseStorage`` is built at boot on
the application and passed to each opt-in client as ``HttpCache(storage=...)``. ``_NoCloseStorage`` wraps the
shared storage per adapter, delegating every method EXCEPT ``close()`` (a no-op)
so a per-adapter ``AsyncCacheTransport.aclose()`` never closes the shared client.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from headgate._cache import _NoCloseStorage


async def test_no_close_storage_delegates_but_skips_close() -> None:
    inner = AsyncMock(name="inner_storage")
    wrapper = _NoCloseStorage(inner)

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

    # close() is a no-op — the inner's close is NEVER awaited (that's the
    # load-bearing guarantee: the shared client survives per-adapter teardown).
    await wrapper.close()
    inner.close.assert_not_awaited()


# The storage-sharing contract under test here is `_NoCloseStorage`: closing a client never closes a shared storage.
