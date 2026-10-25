# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Streaming: the open response :meth:`AsyncBaseClient.stream <headgate.AsyncBaseClient.stream>` yields.

A body too large to hold, or one that never ends (server-sent events), is read through :class:`Stream` as it arrives
instead of being buffered and capped like a verb's.
"""

from headgate.streaming._stream import DEFAULT_MAX_EVENT_SIZE, Stream

__all__ = ["DEFAULT_MAX_EVENT_SIZE", "Stream"]
