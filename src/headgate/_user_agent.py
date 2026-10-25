# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The ``User-Agent`` every client sends: the caller's product token, then headgate's, httpx2's and Python's."""

from __future__ import annotations

import platform
from importlib import metadata

import httpx2

__all__ = ["DEFAULT_USER_AGENT", "headgate_version", "user_agent"]


def headgate_version() -> str:
    """Return the installed headgate version, or ``"unknown"`` for a copy not installed as a distribution."""
    try:
        return metadata.version("headgate")
    except metadata.PackageNotFoundError:
        return "unknown"


#: What a client without ``HttpOptions(user_agent=...)`` sends.
DEFAULT_USER_AGENT = f"headgate/{headgate_version()} httpx2/{httpx2.__version__} python/{platform.python_version()}"


def user_agent(product: str | None) -> str:
    """Return the ``User-Agent`` for a client whose own product token is *product* (``None``: headgate's alone)."""
    return DEFAULT_USER_AGENT if product is None else f"{product} {DEFAULT_USER_AGENT}"
