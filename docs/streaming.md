# Streaming and uploads

Every verb buffers its response, behind the response size cap, before a reader sees it. `stream()` is for a body that
should not be held whole: a large download, or a server-sent event stream that never ends.

## Streams

```python
async with client.stream("exports/2026.csv") as stream:
    async for chunk in stream.aiter_bytes():
        sink.write(chunk)
```

`client.stream(url, *, method="GET", last_event_id=None, **options)` sends the request when the block is entered and
hands it a `Stream` once a 2xx head has arrived. Its options are `headgate.streaming.StreamOptions`: a verb's
[per-call options](configuration.md#per-call-options), `retry=` and `follow_redirects=` included, but `cache_ttl=` and
`max_response_bytes=`, since its body is never buffered. It runs through the same stack and failure ladder as a verb,
except that:

- **a 2xx is the only success.** There is no `expect=`; any other status raises before the block is entered, with a
  bounded head of its body in the error. A 3xx raises unless redirects are followed.
- **the body is never buffered and never capped**, and the response is closed when the block exits, read to the end or
  not.
- **the deadline bounds opening the stream** (every attempt, backoff and the head checks), not reading it. Once the
  response head has arrived, only the per-read timeout bounds the stream, so a stream's own read timeout may be longer
  than its deadline. Wrap the block in `asyncio.timeout` to bound the whole stream.
- **a retry covers opening only.** A failure while the body arrives raises `TransportError` or `ClientTimeoutError`
  from the iteration, after the chunks before it were delivered. Resuming is the caller's decision.
- **the origin's breaker is told of the answer at the response head**, before the block is entered, so a stream that
  is the half-open breaker's probe closes it then, not when the block exits. A 4xx counts as the origin answering, as
  it does for a verb. A network failure mid-body then counts against the origin as one more failure, even when the
  block catches it.
- **an exception the block raises that is not a `ClientError` is the caller's own**, even an `OSError` or an httpx2
  error: it propagates unchanged, the breaker is charged nothing for it, and `CallFinished` reports the status the
  stream opened with and no error.
- the client's `response_check` runs on the response head, before the block is entered.

A `Stream` has `status_code`, `headers` and `request_id`, and three ways to read the body, one per stream:
`aiter_bytes(chunk_size=None)` (decoded bytes as they arrive), `aiter_lines(max_line_size=1 MiB)` (decoded text lines)
and `aiter_events()`.

`aiter_lines` splits where `str.splitlines` does, as httpx2's does, and raises `ResponseTooLargeError` for a line past
`max_line_size` characters (`None` for no limit), before it ends if it never does. It decodes by the response's
`charset`: one that is no text encoding (`base64`, `rot13`) is read as UTF-8, as no `charset` is, and a body the
`charset` cannot decode raises `ResponseParseError`.

## Server-sent events

```python
async with client.events("feed") as stream:
    async for event in stream.aiter_events():
        handle(event.event, event.data)
    resume_from = stream.last_event_id

async with client.events("feed", last_event_id=resume_from) as stream:
    ...
```

`client.events(url, ...)` is `stream()` of a GET with two defaults: it sends `Accept: text/event-stream` unless your
`headers=` name an `Accept`, and each read has no time limit (`math.inf`) unless your `timeout=` sets `read`, since an
event stream may be quiet for minutes. The deadline bounds opening it, as for any stream, and `asyncio.timeout` around
the block bounds the whole of it. It takes every option `stream()` does.

`aiter_events(max_event_size=1 MiB)` parses each event as soon as its blank line arrives, by the WHATWG grammar, into
an `httpx2.ServerSentEvent`: `event`, `data` (multi-line joined), `id` and the `retry` its block named. The body is
UTF-8 whatever its `charset` says, a leading byte order mark is skipped, and a block without data (an `event: ping`
heartbeat, a `retry:` or `id:` alone) is not an event, so `event.data` is always what a `data:` field sent. A response
that does not declare `text/event-stream` raises `ResponseParseError` at once; an event larger than `max_event_size`
(`None` for no limit) raises `ResponseTooLargeError`, however the body is cut into chunks. A comment is never part of an
event, and is dropped as it arrives. The 1 MiB default is `headgate.streaming.DEFAULT_MAX_EVENT_SIZE`.

`stream.last_event_id` is the id the server last set, `None` until it sets one and `""` once it resets it with an
empty `id`. Pass it as `last_event_id=` when re-opening, and the request carries `Last-Event-ID` (none when it is
empty) so the server resumes after it. `stream.retry` is the reconnection time in milliseconds the server last set,
`None` until a `retry:` sets one: it holds across blocks, a `retry:` alone included, as a browser keeps it, while an
event's own `retry` is only the field its block named. A `retry:` past 2**63 - 1 is held at that. Wait that long
before re-opening.

For an event stream that is short and bounded, the buffered `event_stream` reader returns every event in a body, parsed
the same way (see [responses](responses.md#readers)).

## Request bodies

A verb takes **`body=`**, a value a flavor's `writer` wrote: the mirror of `expect=`, built once at import, so
a writer takes only its own type.

```python
from headgate.msgspec import writer

NEW_ORDER = writer(NewOrder)
...
await self.post("orders", body=NEW_ORDER(order), expect=ORDER)
```

It is sent with the writer's `Content-Type` (`application/json` for these codecs) unless the call's `headers=`, or
its view's, sets another; a `Content-Type` among the client's default headers does not count. It is bytes, so the call
stays retryable. `body=` takes one `Body`, whose class says what it is:

| `body=` | Sent as | Sent again on a retry or a 307/308 |
| --- | --- | --- |
| `EncodedBody`, from a `writer` | its bytes, with its `Content-Type` | yes |
| `Json(value)` | JSON of builtins (a codec's `to_builtins` builds them), `application/json` | yes |
| `Form(fields)` | `application/x-www-form-urlencoded` | yes |
| `Multipart(files, fields)` | `multipart/form-data`: files as httpx2 takes them, beside form fields | unless a file cannot be rewound |
| `Raw(content)` | bytes, an async iterable of bytes, or a callable returning one | unless it is an async iterable |

`Raw` takes:

- `bytes`, sent as they are, and retryable;
- an async iterable of `bytes`, streamed once. The call is then **never re-sent**, and a redirect that asks for the
  body again (a 307 or 308, or a 301 answering anything but a POST) is not followed but raised as its 3xx, both with
  a note saying why;
- a callable returning such an iterable: a **factory**, called once for each send, so the call stays retryable and
  redirects are followed. Prefer this for a file.

A `Multipart` file is `{"name": file}`, or a `(filename, file, content_type)` tuple, where the file is bytes, text or a
binary file object; a file object that is neither a regular file nor in memory, such as a pipe, cannot be rewound, so
the call is sent once. `Raw(...).replayable` and `Multipart(...).replayable` say which a body is.

```python
async def chunks(path: Path) -> AsyncIterator[bytes]:
    async with await anyio.open_file(path, "rb") as f:
        while chunk := await f.read(64 * 1024):
            yield chunk


await client.put(t"blobs/{blob_id}", expect=nothing, body=Raw(lambda: chunks(path)))
await client.post(
    "reports", expect=nothing, body=Multipart({"report": ("q3.csv", csv_bytes, "text/csv")}, {"kind": "q"})
)
```

A call has one body, so no two can conflict. `Raw` content that is none of the three (a `str`, which you encode first, a
`bytearray`, a list) raises `TypeError` naming it, and so does a `body=` that is no `Body` (bare `bytes` or a `dict`).
A `CachedTokenAuth` buffers a streamed body before the first send, because a 401 makes it send the request again.
