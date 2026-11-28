# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""Streaming: the open response :meth:`Client.stream <headgate.Client.stream>` yields.

A body too large to hold, or one that never ends (server-sent events), is read through :class:`Stream` as it arrives
instead of being buffered and capped like a verb's. :class:`StreamOptions` are the options ``stream()`` takes, and
:class:`EventOptions` those ``events()`` takes: a stream's less the body.
"""

from headgate._call_options import EventOptions, StreamOptions
from headgate._core import rehome as _rehome
from headgate.streaming._stream import DEFAULT_MAX_EVENT_SIZE, Stream

__all__ = ["DEFAULT_MAX_EVENT_SIZE", "EventOptions", "Stream", "StreamOptions"]

_rehome(__name__, globals())
