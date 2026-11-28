# Codecs

headgate's core imports no serialization library. A **codec** is the one interface between them: how a library reads
bytes into a type and writes a value back. headgate ships two, and any other library plugs in the same way.

## The codecs headgate ships

| Codec | Reads into | Install |
| --- | --- | --- |
| `headgate.msgspec` | `msgspec.Struct` and any msgspec type | `pip install 'headgate[msgspec]'` |
| `headgate.pydantic` | `pydantic.BaseModel` and any pydantic type | `pip install 'headgate[pydantic]'` |

`pip install headgate` alone brings neither. Each module has the same shortcuts, so switching is changing one import:

<!-- docs-test: run -->
```python
from headgate.msgspec import decode, reader, to_builtins, writer

ORDER = reader(Order)  # a reader, for expect=
NEW_ORDER = writer(NewOrder)  # a writer, for body=
```

Both decode strictly (a field that changes type on the wire fails rather than being coerced), ignore fields the type
does not declare, and describe a failure without repeating the body. A message keeps the path to the failure and the
names the type declares, and leaves out anything the body chose: a key of a `dict[str, int]`, an unknown enum value or
tag, an unknown field's name. msgspec drops it (`Invalid enum value - at $.status`; a dict key shows as `$[...]` in the
path), and pydantic shows `[...]` in its place. The library's own error, which may quote the body, is not chained to
the `DecodeError`, so a traceback or `logger.exception` cannot repeat it either. Each module's `CODEC` is the codec
itself, for the functions below.

Each also refuses, with `TypeError` where the reader or writer is built, a type it cannot decode or encode:
`headgate.msgspec.reader` of a pydantic model points to `headgate.pydantic.reader` (and a writer to its `writer`), and
either flavor names a plain class it has no way to build.

## Any codec, the same way

Three functions work on any codec, the two above included:

- `reader_for(codec, Type, *, allow_empty_body=False, refuse=None)` builds a reader for `expect=`;
- `writer_for(codec, Type)` builds a writer for `body=`, sent under the codec's media type;
- `decode_with(codec, content, Type)` decodes bytes that are not a whole response, such as a document nested in
  another.

A body the codec cannot read raises `DecodeError`, which a call raises as `ResponseParseError` with the codec's
description of what was wrong. A body nested too deeply for the library's parser is refused the same way, and
every codec describes it as `nested too deeply`. A library's own validation error never escapes the `ClientError` tree.

## Bringing your own library

A codec is any object with five members, the `Codec` protocol:

| Member | What it is |
| --- | --- |
| `media_type` | what its bytes are: the `Content-Type` a writer sends |
| `errors` | the exceptions it raises for malformed or mistyped bytes, turned into `DecodeError` |
| `decoder(Type)` | a function reading bytes into `Type`, built once per type; `TypeError` for a type it cannot decode |
| `encoder(Type)` | a function writing a value of `Type` as bytes, built once per type |
| `describe(error)` | a message for one of `errors` that never quotes the input, which may hold a credential |

A codec handles the built-in containers too, `dict`, `list` and the scalars inside them, besides its own record
types: `reader_for(..., allow_empty_body=True)` encodes an empty `list[object]` when it builds the reader, and
`check_codec` writes and reads `dict[str, int]` and `dict[str, str]`.

cattrs over orjson, for example. cattrs builds a scalar by calling its type, so `int("1")` would read a numeric string
as a number: the codec registers hooks that take a scalar only as itself, which is the strictness headgate's codecs
keep. A type cattrs has no hook for raises `TypeError` where the decoder is built:

<!-- docs-test: run -->
```python
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import cattrs
import orjson
from typing_extensions import TypeForm

from headgate import reader_for, writer_for

converter = cattrs.Converter(forbid_extra_keys=False)


def _exactly[T](kind: type[T]) -> Callable[[object, object], T]:
    """Return a hook taking a value of *kind* as it is, and refusing any other: no coercion."""

    def structure(value: object, _: object) -> T:
        if isinstance(value, kind) and type(value) is kind:
            return value
        msg = f"expected {kind.__name__}"
        raise ValueError(msg)

    return structure


for scalar in (int, float, str, bool):
    converter.register_structure_hook(scalar, _exactly(scalar))


class CattrsJson:
    """attrs classes and dataclasses, structured by cattrs, as JSON through orjson."""

    media_type = "application/json"
    # orjson's JSONDecodeError is a ValueError, as the hooks' refusal is.
    errors = (cattrs.BaseValidationError, ValueError)

    def decoder[T](self, type_: TypeForm[T], /) -> Callable[[bytes], T]:
        try:
            structure = converter.get_structure_hook(type_)
        except cattrs.errors.StructureHandlerNotFoundError:
            msg = f"cattrs cannot structure {type_!r}: register a structure hook for it"
            raise TypeError(msg) from None

        def decode(content: bytes) -> T:
            # cattrs and orjson are untyped: the hook structures into exactly the type it was fetched for.
            return cast("T", structure(orjson.loads(content), type_))

        return decode

    def encoder[T](self, type_: TypeForm[T], /) -> Callable[[T], bytes]:
        return lambda value: orjson.dumps(converter.unstructure(value, type_))

    def describe(self, error: Exception, /) -> str:
        # The exception's type names the problem; describing only that keeps every value out of the message.
        return type(error).__name__


@dataclass(frozen=True)
class Invoice:
    number: str
    total: int


CODEC = CattrsJson()
INVOICE = reader_for(CODEC, Invoice)
NEW_INVOICE = writer_for(CODEC, Invoice)
```

`TypeForm`, the type of a type expression such as `list[Order]`, comes from `typing_extensions`, which headgate
depends on.

A writer is a function of a value returning an `EncodedBody`: the bytes and the `Content-Type` they are sent with,
`application/json` unless it names another. A body written without a codec is a plain function of that shape:

<!-- docs-test: run -->
```python
import json

from headgate import EncodedBody


def write_order(order: dict[str, object]) -> EncodedBody:
    return EncodedBody(json.dumps(order).encode())
```

A format other than JSON is the same five members with another `media_type`: a msgpack codec says
`"application/msgpack"`, and its writers send that. To read only that type back, declare it on the call's `Expect`:
`Expect(ORDER, accept="application/msgpack")`.

## The promises, checked

`headgate.testing.check_codec` holds a codec to the promises the built-in ones keep, so a codec of your own behaves
like them. Call it from your tests; `record=` is a record type of the codec's own kind holding one field, `value: int`:

<!-- docs-test: run -->
```python
from headgate.testing import check_codec


def test_the_codec_keeps_headgates_promises() -> None:
    check_codec(CODEC, record=Row)
```

It raises `AssertionError` naming the first promise broken:

- `media_type` is a media type;
- `decoder()` raises `TypeError` for a class it cannot decode, so a reader of a type the codec does not support fails
  where it is built, not at its first body as an unreadable response;
- a value it writes reads back equal;
- a string where an `int` is declared is refused, not coerced;
- a body cut short at its middle is refused, so a codec whose bodies end in a newline passes;
- each refusal raises one of `errors`, and neither its description nor its traceback (chained errors included)
  repeats the input, whether a value or a key;
- a field the record does not declare is ignored, since providers add fields without notice (checked only when
  `record=` is given).

Every body it reads is one the codec wrote itself, so the check works for any format.
