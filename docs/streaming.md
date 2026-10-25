# Streaming and uploads

Every verb buffers its response, behind the response size cap, before a reader sees it. `stream()` is for a body that
should not be held whole: a large download, or a server-sent event stream that never ends.

## Streams

```python
async with client.stream("GET", "exports/2026.csv") as stream:
    async for chunk in stream.aiter_bytes():
        sink.write(chunk)
```

`client.stream(method, url, *, retry=..., follow_redirects=..., last_event_id=None, **options)` sends the request when
the block is entered and hands it a `Stream` once a 2xx head has arrived. It runs through the same stack and failure
ladder as a verb, except that:

- **a 2xx is the only success.** There is no `expect=`; any other status raises before the block is entered, with a
  bounded head of its body in the error. A 3xx raises unless redirects are followed.
- **the body is never buffered and never capped**, and the response is closed when the block exits, read to the end or
  not.
- **the deadline bounds opening the stream** (every attempt, backoff and the head checks), not reading it. Each read is
  bounded by the read timeout; wrap the block in `asyncio.timeout` to bound the whole body.
- **a retry covers opening only.** A failure while the body arrives raises `TransportError` or `ClientTimeoutError`
  from the iteration, after the chunks before it were delivered. Resuming is the caller's decision.
- **the origin's breaker spans the whole block**: a network failure mid-body that leaves the block counts against the
  origin, and a clean exit as a success.
- the client's `refusal_check` runs on the response head, before the block is entered.

A `Stream` has `status_code`, `headers` and `request_id`, and three ways to read the body, one per stream:
`aiter_bytes(chunk_size=None)` (decoded bytes as they arrive), `aiter_lines()` (decoded text lines) and
`aiter_events()`.

## Server-sent events

```python
async with client.stream("GET", "events", headers={"Accept": "text/event-stream"}) as stream:
    async for event in stream.aiter_events():
        handle(event.event, event.data)
    resume_from = stream.last_event_id

async with client.stream("GET", "events", last_event_id=resume_from) as stream:
    ...
```

`aiter_events(max_event_size=1 MiB)` parses each event as soon as its blank line arrives, with httpx2's WHATWG parser:
`event`, `data` (multi-line joined), `id` and `retry`. A response that does not declare `text/event-stream` raises
`ResponseParseError` at once; an event larger than `max_event_size` (`None` for no limit) raises
`ResponseTooLargeError`. `stream.last_event_id` is the last `id` seen; pass it as `last_event_id=` when re-opening, and
the request carries `Last-Event-ID` so the server resumes after it.

For an event stream that is short and bounded, the buffered `event_stream` reader returns the `data` of every event in
a body (see [responses](responses.md#readers)).

## Request bodies

A verb takes `json=` (build it with a codec's `to_builtins`), `data=` (form fields), or one of:

- **`content=`**, a raw body:
  - `bytes`, sent as they are, and retryable;
  - an async iterable of `bytes`, streamed once. The call is then **never re-sent**, and a 307 or 308 that asks for the
    body again is not followed but raised as its 3xx, both with a note saying why;
  - a callable returning such an iterable: a **factory**, called once for each send, so the call stays retryable and
    redirects are followed. Prefer this for a file.
- **`files=`**, a multipart body as httpx2 takes it (`{"name": file}`, or `(filename, file, content_type)` tuples), with
  `data=` as its form fields.

```python
async def chunks(path: Path) -> AsyncIterator[bytes]:
    async with await anyio.open_file(path, "rb") as f:
        while chunk := await f.read(64 * 1024):
            yield chunk


await client.put(t"blobs/{blob_id}", expect=nothing, content=lambda: chunks(path))
await client.post("reports", expect=nothing, files={"report": ("q3.csv", csv_bytes, "text/csv")}, data={"kind": "q"})
```

`content=` combines with no other body option, and `files=` cannot carry `json=`: both raise `ValueError` rather than
let httpx2 drop one silently. A `CachedTokenAuth` buffers a streamed body before the first send, because a 401 makes it
send the request again.
