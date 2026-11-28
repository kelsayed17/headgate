# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The pydantic codec: strict JSON validation into pydantic models, and request bodies written back.

:data:`CODEC` is pydantic as a :class:`~headgate.Codec`, with the same shortcuts as :mod:`headgate.msgspec`, so
switching codec is changing one import
(``from headgate.pydantic import reader, writer, to_builtins``). Wire models are frozen, strict ``BaseModel``
classes::

    class Row(BaseModel):
        model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

Strictness parity with the msgspec flavor is held by tests: a JSON string where an ``int`` is declared fails, an
unknown field is ignored, and every failure is the shared :class:`~headgate.errors.DecodeError`.

A ``DecodeError``'s message names the first few failures by location and reason, and how many there were. It never
repeats the input: ``str(ValidationError)`` lists every failure with the value that failed, so a body of 5,000 bad rows
makes a message of most of a megabyte, and a token field that failed validation is quoted back in full. A location
shows the names the type declares and ``[...]`` for a key of the input's own (a ``dict[str, int]``'s), as msgspec's
``$[...]`` does, and an unknown tag is ``[...]`` too.

Requires the ``pydantic`` extra: ``pip install headgate[pydantic]``.
"""

from headgate._core import rehome as _rehome
from headgate._pydantic import CODEC, PydanticCodec, decode, reader, to_builtins, writer

__all__ = ["CODEC", "PydanticCodec", "decode", "reader", "to_builtins", "writer"]

_rehome(__name__, globals())
