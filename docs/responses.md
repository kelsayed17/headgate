# Declared responses

A client method is one provider endpoint, and it returns that endpoint's documented outcome as a typed value or raises
a `ClientError`. So every call says, where it is made, what it can get back: `expect=` takes a **reader** for any 2xx,
or an **`Expect`** that adds the exact non-2xx statuses the provider documents as answers. Anything the call did not
declare raises. There is no raw mode, so no call can receive a status it did not read on purpose.

## Readers

A reader is any `Callable[[httpx2.Response], T]` (the `Reader[T]` alias). It receives the whole response, body already
read and capped, because some readers need a header too. headgate ships these:

| Reader | Returns | For |
| --- | --- | --- |
| `decoder(Type)` from `headgate.msgspec` or `headgate.pydantic` | the decoded value | a JSON body |
| `json_reader(decode)` | what `decode(body_bytes)` returns | a codec of your own |
| `as_bytes` | the body, undecoded | CSV, ZIP, PDF, a dump another parser reads |
| `nothing` | `None` | a void write (`DELETE`, a cancel) or a `HEAD` |
| `event_stream` | the `data` of each server-sent event, in order | a buffered `text/event-stream` body |

A reader that finds the body is not the shape it expected raises: a codec raises `DecodeError`, and the `ValueError`,
`LookupError` or `TypeError` hand-written code over `response.json()` raises counts the same. On a 2xx the client turns
it into `ResponseParseError`, naming the status, the request line and the reader's diagnosis. A reader never builds a
`ClientError` itself: the client does, so the error carries the real status and the client's name.

`nothing` is a claim about the wire, so declare it only for an endpoint documented to send no body you need. Await
such a call as a statement.

## Declared non-2xx answers

```python
from http import HTTPStatus

from headgate import Expect, nothing
from headgate.msgspec import decoder

SERIES_OR_NONE = Expect(decoder(Series)).on(HTTPStatus.NOT_FOUND, nothing)
ORDER_OR_REJECTION = Expect(decoder(Order)).on(HTTPStatus.UNPROCESSABLE_ENTITY, decoder(Rejection))
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
2. **Host check.** The client's `HttpOptions(refusal_check=...)`, if any, runs on the final response.
3. **Declared.** A status declared with `on` goes to its reader; any other 2xx to the success reader.
4. **Status ladder.** Any other status raises: 401 and 403 as `AuthError`, the rest as `UpstreamStatusError`.

A declared non-2xx whose body its reader cannot read falls through to the ladder with its real status. A declaration
covers the body shape it names, so a business 403 you declared never swallows a real permission 403.

A redirect the call does not follow (`follow_redirects=False`) is a 3xx like any other: declare it, for example
`.on(HTTPStatus.FOUND, read_location)`, or it raises.

## Content negotiation

`Expect(reader, accept="application/json")` sends that `Accept` header (unless the call sets one in `headers=`) and
checks every non-empty response's `Content-Type` before the reader runs. Media types compare without parameters and
case-insensitively, `*/*` and `type/*` match as in `Accept`, and a structured suffix matches its base type, so
`application/problem+json` is `application/json`. A mismatch on a 2xx raises `ResponseParseError` with the status and
the content type; on a declared non-2xx it falls through to the ladder. `accept` may list several types,
comma-separated.

`Expect(..., headers={...})` sends request headers the declaration needs; a header the call sets itself wins. This is
how [conditional reads](caching.md#conditional-reads) send their preconditions.

## Refusals inside a body

Some providers answer a failure with a 200 and an error envelope. A reader raises `RefusedError` for that, and the
client turns it into the `ClientError` its `kind` names (`UpstreamStatusError` by default, `AuthError`,
`RateLimitError`), with the status the wire sent and the provider's `code`. The simplest way is a `decoder`'s `refusal_check`,
which runs on the decoded value and returns a refusal or `None`:

```python
import msgspec

from headgate import RateLimitError, RefusedError
from headgate.msgspec import decoder


class Envelope(msgspec.Struct, frozen=True):
    status: str
    code: str | None = None
    rows: list[dict[str, object]] = []


def refusal_in(envelope: Envelope) -> RefusedError | None:
    if envelope.code == "QUOTA_EXCEEDED":
        return RefusedError("daily quota exhausted", code=envelope.code, kind=RateLimitError)
    return None


ROWS = decoder(Envelope, refusal_check=refusal_in)
```

Refuse only codes every consumer of the endpoint would read as failure (credentials, quota, a malformed request) and
leave a code about the subject (an unknown instrument, a rejected order) in the value for the caller. A refusal raised
inside a 200 bypasses the retry backoff and the breaker, because a 200 looks healthy to both, so retrying it is the
caller's decision.

A refusal written in the status line or headers rather than the body (a key-error redirect to a sign-in page) belongs
to a host-wide `HttpOptions(refusal_check=...)`: a function of the response that returns a `RefusedError` or `None`.
It sees only the final response, so it requires `follow_redirects=False`, and a call that asks to follow redirects on
such a client raises `ValueError`.

## Codec flavors

The core imports no codec. `headgate.msgspec` and `headgate.pydantic` expose the same four functions:

- `decode(content, Type, *, strict=True)`: bytes to a value, raising `DecodeError`;
- `decoder(Type, *, strict=True, allow_empty_body=False, refusal_check=None)`: a reader built on `decode`;
- `encoder(Type, *, drop_none=False)`: the mirror of `decoder`, a writer whose `body=WRITER(value)` sends the value
  as JSON (see [request bodies](streaming.md#request-bodies));
- `to_builtins(value, *, drop_none=False)`: a struct or model (or a list or mapping of them) as builtins for `json=` or
  `data=`, with field renames applied and `drop_none=True` removing `None` fields recursively.

`Type` is any type expression, so one function serves an envelope (`decoder(Page)`), a bare array
(`decoder(list[Row])`), a map, or a genuine union (`decoder(list[A] | A)`), and the reader is typed exactly that.

Drift detection is strict typing: a field that changes type on the wire fails, because it would otherwise corrupt
values silently. Unknown fields are ignored, because providers add fields routinely and a field nobody models is a field
nobody reads; a model can still forbid them. A `DecodeError`'s message is the codec's diagnosis, bounded, and never
quotes an input value.

`allow_empty_body=True` reads an empty body as `[]`, for a list endpoint whose "no data" answer is a zero-byte 200.
Anywhere else an empty 200 is corruption, and decoding it fails.
