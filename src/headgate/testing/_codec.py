# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""A conformance check for a :class:`~headgate.Codec`: the promises headgate's own codecs keep, asked of any other.

Every body it reads is one the codec itself wrote, so the check works for any format, JSON or not. It needs only the
type expressions every serialization library reads (``dict[str, int]``, ``dict[str, str]``), plus, for the
unknown-field promise, a record type of the codec's own kind.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from headgate._codec import decode_with
from headgate.errors import DecodeError

if TYPE_CHECKING:
    from typing_extensions import TypeForm

    from headgate._codec import Codec

__all__ = ["check_codec"]

#: A string no codec's message may repeat, whether a body holds it as a value or as a key: either might be a credential.
_PLANTED: Final = "hg-secret-0b7f3a9c"

#: A media type: a ``type/subtype`` pair of RFC 9110 tokens, with any parameters.
_MEDIA_TYPE: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+/[!#$%&'*+\-.^_`|~0-9A-Za-z]+(\s*;.*)?")


def _refused(codec: Codec, content: bytes, type_: TypeForm[object], what: str) -> DecodeError:
    """Return the :class:`DecodeError` *codec* raises reading *content* as *type_*, or fail saying it took *what*."""
    try:
        value = decode_with(codec, content, type_)
    except DecodeError as exc:
        return exc
    except Exception as exc:
        msg = (
            f"{type(codec).__name__} raised {type(exc).__name__} for {what}, which is not one of its errors "
            f"{[error.__name__ for error in codec.errors]}: a call would raise it outside the ClientError tree"
        )
        raise AssertionError(msg) from exc
    msg = f"{type(codec).__name__} accepted {what} as {value!r}: it must refuse it"
    raise AssertionError(msg)


def check_codec(codec: Codec, *, record: TypeForm[object] | None = None) -> None:
    """Check that *codec* keeps the promises headgate's own codecs keep; raise ``AssertionError`` at the first broken.

    * its ``media_type`` is a media type;
    * a value it writes reads back equal;
    * a value of the wrong type is refused, not coerced: a string where an ``int`` is declared;
    * malformed bytes (a body cut short at its middle, so a final delimiter is not all that is lost) are refused;
    * each refusal raises one of its ``errors``, so a call raises it as ``ResponseParseError``, and its description
      never repeats the input, a value or a key, which may carry a credential;
    * with *record*, a type of the codec's own kind holding one field ``value: int``, a field the type does not declare
      is ignored, since providers add fields without notice.

    Call it from a test of the codec: ``check_codec(MY_CODEC, record=Row)``.
    """
    name = type(codec).__name__
    if not _MEDIA_TYPE.fullmatch(codec.media_type):
        msg = f"{name}.media_type {codec.media_type!r} is not a media type, such as 'application/json'"
        raise AssertionError(msg)

    counts = codec.encoder(dict[str, int])({"a": 1})
    if (read := decode_with(codec, counts, dict[str, int])) != {"a": 1}:
        msg = f"{name} wrote {{'a': 1}} and read back {read!r}"
        raise AssertionError(msg)

    for planted in ({"a": _PLANTED}, {_PLANTED: "a"}):
        mistyped = codec.encoder(dict[str, str])(planted)
        refusal = _refused(codec, mistyped, dict[str, int], "a string where an int is declared")
        if _PLANTED in str(refusal):
            msg = f"{name}.describe() repeats the input ({str(refusal)!r}): a body's value or key may be a credential"
            raise AssertionError(msg)

    _refused(codec, counts[: len(counts) // 2], dict[str, int], "a cut-short body")

    if record is not None:
        extra = codec.encoder(dict[str, object])({"value": 1, "added_by_the_provider": "x"})
        try:
            decoded = decode_with(codec, extra, record)
        except DecodeError as exc:
            msg = f"{name} refused a body with a field {record!r} does not declare: it must ignore it"
            raise AssertionError(msg) from exc
        if getattr(decoded, "value", None) != 1:
            msg = f"{name} read {decoded!r} from a body with a field {record!r} does not declare: it must ignore it"
            raise AssertionError(msg)
