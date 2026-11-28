# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The msgspec codec: strict JSON decoding into ``msgspec.Struct`` types, and request bodies written back.

:data:`CODEC` is msgspec as a :class:`~headgate.Codec`. A client usually reaches it through the shortcuts here
(``from headgate.msgspec import reader, writer``): :func:`decode` is the bytes-level primitive, :func:`reader`
builds a response reader on it and :func:`writer` its mirror, a request-body writer; :func:`to_builtins` prepares
builtins for ``body=Json(...)`` or ``Form(...)``. Every decode failure is the shared
:class:`~headgate.errors.DecodeError`.

A ``DecodeError``'s message is msgspec's own diagnosis (the expected type and the path), bounded, with the input
msgspec quotes left out: an unknown enum value or tag, and an unknown field's name. A dict key in the path is already
``$[...]``.

Drift detection is strict **typing**, not unknown-field rejection. ``strict=True`` catches a field that changes type
on the wire, which silently corrupts values. ``forbid_unknown_fields`` would catch a field appearing, which providers
do routinely and without notice: rejecting one turns a harmless upstream addition into a failure on every call, while
ignoring it costs nothing, since a field nobody models is a field nobody reads. A struct can still opt in with
``forbid_unknown_fields=True`` where noticing a new field is worth that risk.

``type_`` parameters are annotated ``TypeForm[T]`` (PEP 747), so a union or a generic alias such as ``list[A] | A``
types the result as exactly that expression.

Requires the ``msgspec`` extra: ``pip install headgate[msgspec]``.
"""

from headgate._core import rehome as _rehome
from headgate._msgspec import CODEC, MsgspecCodec, decode, reader, to_builtins, writer

__all__ = ["CODEC", "MsgspecCodec", "decode", "reader", "to_builtins", "writer"]

_rehome(__name__, globals())
