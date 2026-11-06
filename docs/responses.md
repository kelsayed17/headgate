# Declared responses

A client method is one provider endpoint, and it returns that endpoint's documented outcome as a typed value or raises
a `ClientError`. So every call says, where it is made, what it can get back: `expect=` takes a **reader** for any 2xx,
or an **`Expect`** that adds the exact non-2xx statuses the provider documents as answers. Anything the call did not
declare raises. There is no raw mode, so no call can receive a status it did not read on purpose.

## Readers

A reader is a `Reader[T]`: `Reader(fn)` around a function of the response that returns a `T`. It receives the whole
response, body already read and capped, because some readers need a header too. headgate ships these:

| Reader | Returns | For |
| --- | --- | --- |
| `reader(Type)` from `headgate.msgspec` or `headgate.pydantic` | the decoded value | a JSON body |
| `body_reader(decode)` | what `decode(body_bytes)` returns | a decoder of your own in any format: JSON, XLS, fixed-width TXT, CSV |
| `as_json` | the body as builtins, typed `object` | JSON without a codec, on a core-only install |
| `as_bytes` | the body, undecoded | CSV, ZIP, PDF, a dump another parser reads |
| `nothing` | `None` | a void write (`DELETE`, a cancel) or a `HEAD` |
| `event_stream` | each server-sent event (`event`, `data`, `id`, `retry`), in order | a buffered `text/event-stream` body |

A reader that finds the body is not the shape it expected raises: a codec raises `DecodeError`, and the `ValueError`,
`LookupError` or `TypeError` hand-written code over `response.json()` raises, or nesting too deep to parse, counts the
same. On a 2xx the client turns it into `ResponseParseError`, naming the status, the request line and the reader's
diagnosis. A reader never builds a `ClientError` itself: the client does, so the error carries the real status and the
client's name.

`body_reader` turns a hand-written bytes decoder, the kind its tests feed fixture files, into a reader. Name each
reader once, at module level, and pass a fixed argument with `functools.partial`:

```python
from functools import partial
from typing import Final

from headgate import body_reader

ALLOTMENTS: Final = body_reader(decode_allotments)
STATEMENTS: Final = body_reader(partial(decode_statements, table=Statement))
```

It hands `decode` an empty body as it is, unless `empty=` names the bytes to decode instead: `body_reader(decode,
empty=b"[]")` reads an empty body as the empty JSON array. Its `refuse=` runs on the decoded value, as a codec reader's
does (see [a refusal inside the body](#a-refusal-inside-the-body)).

`as_json` reads JSON into builtins with the standard library's `json.loads`, so a core-only install can read JSON
without a codec. It is typed `object`, the honest type of `json.loads`: narrow it with `isinstance` or a `match`. It
reads strict RFC 8259 JSON, as `headgate.msgspec`'s reader does, and refuses a body that is not UTF-8 (a BOM, UTF-16,
UTF-32), `NaN` and `Infinity`, a number too large for a float, an integer of over 4,300 digits, a string holding a lone
surrogate, and nesting too deep to parse. Each refusal is a `DecodeError` naming the position, never the input.

`nothing` is a claim about the wire, so declare it only for an endpoint documented to send no body you need. Await
such a call as a statement.

A reader of your own wraps its function in `Reader`, or puts `@Reader` over the `def`:

```python
import httpx2

from headgate import Reader

REQUEST_ID = Reader(lambda response: response.headers["X-Request-Id"], name="request_id")


@Reader
def location(response: httpx2.Response) -> str:
    return response.headers["Location"]


repr(location)  # 'Reader(location)'
```

A reader's `repr` names its function, or the `name=` it was given. `expect=`, `Expect` and `conditional` take only a
`Reader` (or, for `expect=`, an `Expect`) and raise `TypeError` for anything else, such as a model class (`expect=Order`
where `expect=reader(Order)` was meant) or a bare function, which are callable too.

## Declared non-2xx answers

```python
from http import HTTPStatus

from headgate import Expect, nothing
from headgate.msgspec import reader

SERIES_OR_NONE = Expect(reader(Series)).on(HTTPStatus.NOT_FOUND, nothing)
ORDER_OR_REJECTION = Expect(reader(Order)).on(HTTPStatus.UNPROCESSABLE_ENTITY, reader(Rejection))
```

`Expect(ok).on(status, reader)` returns a new `Expect` whose result type widens to `T | E`. Build them once, at module
level: `on` validates where it is called, so a bad declaration fails at import. It refuses:

- a status outside 100-599;
- 401 and 407, since the credential is ours, and 429 and every 5xx, which the transport owns through retries, the rate
  budget and the breaker ("we could not ask" is never a documented answer);
- a status already declared.

There are no ranges (`4XX`) and no default: a range is a claim about statuses nobody documented.

A response is read in this order:

1. **Transport.** A network failure, a 429 or 5xx that outlasted the retries, or an open breaker raises.
2. **Response check.** The client's `HttpOptions(response_check=...)`, if any, runs on the final response.
3. **Declared.** A status declared with `on` goes to its reader; any other 2xx to the success reader.
4. **Status ladder.** Any other status raises: 401 and 403 as `AuthError`, the rest as `UpstreamStatusError`.

A declared non-2xx whose body its reader cannot read falls through to the ladder with its real status. A declaration
covers the body shape it names, so a business 403 you declared never swallows a real permission 403.

A redirect the call does not follow (`follow_redirects=False`) is a 3xx like any other: declare it, for example
`.on(HTTPStatus.FOUND, read_location)`, or it raises.

## Content negotiation

`Expect(reader, accept="application/json")` sends that `Accept` header (unless the call or its view sets one in
`headers=`) and checks every non-empty response's `Content-Type` before the reader runs. Media types compare without
parameters and case-insensitively, `*/*` and `type/*` match as in `Accept`, and a structured suffix matches its base
type, so `application/problem+json` is `application/json`. A mismatch on a 2xx raises `ResponseParseError` with the
status and the content type; on a declared non-2xx it falls through to the ladder. `accept` may list several types,
comma-separated.

`Expect(..., headers={...})` sends request headers the declaration needs; a header the call or its view sets in
`headers=` wins. This is how [conditional reads](caching.md#conditional-reads) send their preconditions.

## A refusal inside the body

Some providers answer a failure with a 200 and an error envelope. A reader raises `RefusedError` for that, and the
client turns it into the `ClientError` its `kind` names (`UpstreamStatusError` by default, `AuthError`,
`RateLimitError`), with the status the wire sent and the provider's `code`. The simplest way is a reader's `refuse`,
which runs on the decoded value and returns a refusal or `None`:

```python
import msgspec

from headgate.errors import RateLimitError, RefusedError
from headgate.msgspec import reader


class Envelope(msgspec.Struct, frozen=True):
    status: str
    code: str | None = None
    rows: list[dict[str, object]] = []


def refusal_in(envelope: Envelope) -> RefusedError | None:
    if envelope.code == "QUOTA_EXCEEDED":
        return RefusedError("daily quota exhausted", code=envelope.code, kind=RateLimitError)
    return None


ROWS = reader(Envelope, refuse=refusal_in)
```

Refuse only codes every consumer of the endpoint would read as failure (credentials, quota, a malformed request) and
leave a code about the subject (an unknown instrument, a rejected order) in the value for the caller. A refusal raised
inside a 200 bypasses the retry backoff and the breaker, because a 200 looks healthy to both, so retrying it is the
caller's decision.

## A check of the response

A refusal written in the status line or headers rather than the body (a key-error redirect to a sign-in page) belongs
to a host-wide `HttpOptions(response_check=...)`: a function of the response that returns a `RefusedError` (from
`headgate.errors`) or `None` (its type is `ResponseCheck`). It sees only the final response, so it requires
`follow_redirects=False`, and a call that asks to follow redirects on such a client raises `ValueError`.

## Codecs

The core imports no serialization library. `headgate.msgspec` and `headgate.pydantic` are two
[codecs](codecs.md), and expose the same four shortcuts:

- `decode(content, Type, *, strict=True)`: bytes to a value, raising `DecodeError`;
- `reader(Type, *, strict=True, allow_empty_body=False, refuse=None)`: a reader built on `decode`;
- `writer(Type, *, drop_none=False)`: the mirror of `reader`, a writer whose `body=WRITER(value)` sends the value
  as JSON (see [request bodies](streaming.md#request-bodies));
- `to_builtins(value, *, drop_none=False)`: a struct or model (or a list or mapping of them) as builtins for `json=` or
  `data=`, with field renames applied and `drop_none=True` removing `None` fields recursively.

`Type` is any type expression, so one function serves an envelope (`reader(Page)`), a bare array
(`reader(list[Row])`), a map, or a genuine union (`reader(list[A] | A)`), and the reader is typed exactly that.

Drift detection is strict typing: a field that changes type on the wire fails, because it would otherwise corrupt
values silently. Unknown fields are ignored, because providers add fields routinely and a field nobody models is a field
nobody reads; a model can still forbid them. A `DecodeError`'s message is the codec's diagnosis, bounded, and never
quotes an input value or key.

`allow_empty_body=True` reads an empty body as `[]`, for a list endpoint whose "no data" answer is a zero-byte 200.
Anywhere else an empty 200 is corruption, and decoding it fails.
