# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The optional curl transport: ``from headgate.curl import AsyncCurlTransport`` (needs ``headgate[curl]``)."""

from headgate._curl import AsyncCurlTransport

__all__ = ["AsyncCurlTransport"]
