# Codecs

headgate's core imports no serialization library. A **codec** is the one interface between them: how a library reads
bytes into a type and writes a value back. headgate ships two, and any other library plugs in the same way.

## The codecs headgate ships

| Codec | Reads into | Install |
| --- | --- | --- |
| `headgate.msgspec` | `msgspec.Struct` and any msgspec type | `pip install 'headgate[msgspec]'` |
| `headgate.pydantic` | `pydantic.BaseModel` and any pydantic type | `pip install 'headgate[pydantic]'` |

`pip install headgate` alone brings neither. Each module has the same shortcuts, so switching is changing one import:

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
| `decoder(Type)` | a function reading bytes into `Type`, built once per type |
| `encoder(Type)` | a function writing a value of `Type` as bytes, built once per type |
| `describe(error)` | a message for one of `errors` that never quotes the input, which may hold a credential |

cattrs over orjson, for example:

```python
import cattrs
import orjson

from headgate import reader_for, writer_for

converter = cattrs.Converter(forbid_extra_keys=False)


class CattrsJson:
    """attrs classes and dataclasses, structured by cattrs, as JSON through orjson."""

    media_type = "application/json"
    errors = (orjson.JSONDecodeError, cattrs.BaseValidationError)

    def decoder(self, type_):
        return lambda content: converter.structure(orjson.loads(content), type_)

    def encoder(self, type_):
        return lambda value: orjson.dumps(converter.unstructure(value, type_))

    def describe(self, error):
        # The exception's type names the problem; describing only that keeps every value out of the message.
        return type(error).__name__


CODEC = CattrsJson()
ORDER = reader_for(CODEC, Order)
NEW_ORDER = writer_for(CODEC, NewOrder)
```

A writer is a function of a value returning an `EncodedBody`: the bytes and the `Content-Type` they are sent with,
`application/json` unless it names another. A body written without a codec is a plain function of that shape:

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

```python
from headgate.testing import check_codec


def test_the_codec_keeps_headgates_promises() -> None:
    check_codec(CODEC, record=Row)
```

It raises `AssertionError` naming the first promise broken:

- `media_type` is a media type;
- a value it writes reads back equal;
- a string where an `int` is declared is refused, not coerced;
- a body cut short at its middle is refused, so a codec whose bodies end in a newline passes;
- each refusal raises one of `errors`, and neither its description nor its traceback (chained errors included)
  repeats the input, whether a value or a key;
- a field the record does not declare is ignored, since providers add fields without notice (checked only when
  `record=` is given).

Every body it reads is one the codec wrote itself, so the check works for any format.
