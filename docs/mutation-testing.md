# Mutation testing

Coverage says a line ran; mutation testing says a test would notice if it were wrong. [mutmut](https://mutmut.readthedocs.io/)
rewrites `src/` one small change at a time (a flipped comparison, a dropped argument, a changed string) and runs the
suite against each rewrite, a *mutant*. A mutant the suite fails on is killed; one it passes survives, and is either a
missing test or a change no input can tell apart from the original (an *equivalent* mutant).

## Running it

The configuration is `[tool.mutmut]` in `pyproject.toml`. Mutation runs leave out the tests of the source's own shape
(the API-surface snapshot, the import boundary, the no-logging and no-environment contracts), which read the rewritten
files.

```sh
uv run mutmut run                       # every mutant, about 20 minutes on 10 workers
uv run mutmut run "headgate._failures*" # one module's mutants, while working on it
uv run mutmut results                   # the survivors
uv run mutmut show <name>               # one mutant's diff
```

Delete `mutants/` if a run gets confused after files move. `--max-children` sets the number of workers.

mutmut 3.8.0's test harness reads pytest's private `Item._nodeid`, which pytest 9.2 removed: until a mutmut release
catches up, run it on pytest 9.1, as `uv run --with pytest==9.1.1 mutmut run`.

On macOS, mutmut reports some mutants as `segfault`: the mutant made a test take a path through the system TLS stack
(an SSL context built without verification, say) in a forked worker, where Security.framework aborts the process. The
same tests fail on those mutants when run in a plain process, so they are counted as killed.

mutmut forks every worker from the process that collected the tests' statistics, so what that process cached stays
cached for each mutant. A function reached only through a warm cache (the names `headgate.pydantic` collects from a
schema the first time it compiles a type) is never run under a mutant, which then survives; test such a function
directly.

## Latest score

A full run on 2026-10-27 (mutmut 3.8.0 on pytest 9.1.1, 10 workers, about 20 minutes) generated 6,649 mutants. It ran
against the source of headgate 0.9.0 at [`c887aa3`](https://github.com/kelsayed17/headgate/tree/c887aa3/src/headgate),
with the tests written for that run's survivors:

| Outcome | Mutants |
| --- | --- |
| killed by a failing test | 6,251 |
| killed by a crash in the forked worker (`segfault`, see above) | 136 |
| killed by a timeout | 105 |
| survived, every one equivalent (listed below) | 157 |

That is 6,492 of 6,649 detected (97.6%), and every mutant that changes behavior is detected. The same run before those
tests left 233 survivors; the 76 that changed behavior each now fail a test. They were in the event-stream parser (a
body cut between a CR and its LF, a byte order mark past the first line, the exact bounds of `max_event_size`), a cache
partition built from more than one credential source, a GET or HEAD whose one-shot body a redirect would send again, a
`json=` walk of POSTs, a hedge or hedged retry the rate budget refused, the rate budget's edges at a pause and at a
deadline, `TestClock`'s settling turns, and a validator's own error under a pydantic error type that quotes the input,
among others. Ten of them, in the schema walk behind `headgate.pydantic`'s redaction, survived only because of the warm
cache described above.

## Equivalent mutants

Every survivor was checked, and each one left is equivalent: no input distinguishes them. Most fall in a
few families:

- **header-name case**: httpx2 headers are case-insensitive, so `headers.get("ETag")` and `headers.get("etag")` read
  the same field;
- **codec, distribution and method names**: `"ascii"`/`"ASCII"`, `"latin-1"`/`"LATIN-1"`, `"utf-8"`/`"UTF-8"`,
  `metadata.version("HEADGATE")` and `MethodFilter({"get"})` (names are normalized);
- **falsy stand-ins**: `None` for `False` or `""` where the value is only tested for truth;
- **defaults spelt out**: an argument equal to the callee's default (`to_bytes(8, "big")`, `zip(..., strict=False)`,
  `secrets.token_bytes(32)`, `copy_with(userinfo=None)`, which clears userinfo exactly as `b""` does);
- **unreachable fallbacks**: a default for a value the code never sees missing (a header read with `""` or
  `"XXXX"` when absent, both of which parse to nothing);
- **type arguments only the type checker reads**: `typing.cast`'s first argument, and the type expression msgspec's
  encoder is handed.

| Module | Mutant | Why it is equivalent |
| --- | --- | --- |
| `headgate.health` | `breaker_states #12` | two snapshots of equal rank are equal (state and refusing are all a snapshot holds) |
| `headgate._responses` | `accepts #12` | header-name case |
| `headgate._responses` | `accepts #13` | a missing content-type read as `XXXX` is no media type, like `""` |
| `headgate._deprecation` | `_link #3` | header-name case |
| `headgate._deprecation` | `deprecation_of #4` | header-name case |
| `headgate._deprecation` | `deprecation_of #5` | header-name case |
| `headgate._deprecation` | `deprecation_of #9` | header-name case |
| `headgate._deprecation` | `deprecation_of #10` | header-name case |
| `headgate._user_agent` | `headgate_version #3` | distribution names are normalized, so `HEADGATE` finds `headgate` |
| `headgate.pydantic` | `_adapter #1` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `_adapter #5` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `_adapter #6` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `_adapter #7` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `PydanticCodec.describe #2` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `PydanticCodec.describe #6` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `PydanticCodec.describe #7` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `PydanticCodec.describe #8` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `PydanticCodec.describe #13` | the message never reads an error's `input` |
| `headgate.pydantic` | `PydanticCodec.describe #14` | the message never reads an error's `url` |
| `headgate.pydantic` | `PydanticCodec.describe #15` | `include_context=True` is the default |
| `headgate.pydantic` | `PydanticCodec.describe #16` | the message never reads an error's `input` |
| `headgate.pydantic` | `PydanticCodec.describe #17` | the message never reads an error's `url` |
| `headgate.pydantic` | `_reason #17` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `_reason #21` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `_reason #22` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `_reason #23` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `to_builtins #6` | pydantic-core's `to_jsonable_python` serializes a model by alias already |
| `headgate.msgspec` | `encoder #3` | `MsgspecCodec.encoder` drops the type expression: msgspec encodes a value by its own type |
| `headgate._client` | `AsyncBaseClient.with_options #17` | `_owns_pool` is only tested for truth |
| `headgate._client` | `AsyncBaseClient.stream #12` | header-name case |
| `headgate._client` | `AsyncBaseClient.stream #13` | header-name case |
| `headgate._client` | `AsyncBaseClient._one_key_for_every_page #7` | the name is lower-cased on the next step |
| `headgate._client` | `AsyncBaseClient._one_key_for_every_page #8` | the name is lower-cased on the next step |
| `headgate._client` | `AsyncBaseClient._dispatch #30` | header-name case |
| `headgate._client` | `_query_value #2` | httpx2 sends any other value as its `str()`, exactly what the function returns; it exists for the static type |
| `headgate._failures` | `CallSite._request_id #3` | a missing header read as `None` is as falsy as `""` |
| `headgate._failures` | `CallSite._request_id #5` | a missing header read as `None` is as falsy as `""` |
| `headgate._failures` | `body_snippet #7` | httpx2's `Response.encoding` is never falsy (it falls back to utf-8 itself) |
| `headgate._failures` | `body_snippet #8` | httpx2's `Response.encoding` is never falsy (it falls back to utf-8 itself) |
| `headgate._failures` | `problem_of #10` | header-name case |
| `headgate._failures` | `problem_of #11` | a missing content-type read as `xxxx` is not a problem document, like `""` |
| `headgate._failures` | `parse_error #15` | header-name case |
| `headgate._failures` | `mint_error #10` | an empty body snippet is treated as absent, exactly like `None` |
| `headgate._curl` | `_httpx_response #6` | httpx2 headers' `in` is case-insensitive |
| `headgate._curl` | `_httpx_response #31` | codec names are case-insensitive |
| `headgate._curl` | `_httpx_response #44` | httpx2 attaches the request to an error raised while it reads a body |
| `headgate._curl` | `_CurlByteStream.__init__ #1` | httpx2 attaches the request to an error raised while it reads a body |
| `headgate._redirects` | `_redirect_method #20` | the only caller compares the method with the request's `POST`, which any other string differs from as `GET` does |
| `headgate._redirects` | `_redirect_method #21` | the only caller compares the method with the request's `POST`, which any other string differs from as `GET` does |
| `headgate._redirects` | `refuse_unreplayable_redirect #15` | header-name case |
| `headgate._redirects` | `refuse_unreplayable_redirect #16` | header-name case |
| `headgate.errors` | `Problem.__setstate__ #3` | `strict=None` is as false as `strict=False`, the default |
| `headgate.errors` | `Problem.__setstate__ #6` | `strict=False` is `zip`'s default |
| `headgate._body` | `read_capped #6` | header-name case |
| `headgate.otel` | `OpenTelemetryObserver.__init__ #26` | the OpenTelemetry SDK lower-cases instrument names |
| `headgate._auth` | `_read_hop #13` | the status of the response built only to read the hop's body is never read |
| `headgate._auth` | `BearerAuth.__init__ #3` | header-name case |
| `headgate._auth` | `BearerAuth.__init__ #4` | header-name case |
| `headgate._auth` | `CachedTokenAuth.__init__ #3` | `token_bytes(None)` draws the default 32 bytes |
| `headgate._auth` | `CachedTokenAuth.__init__ #4` | a 33-byte random identity is as unique as a 32-byte one; it is only compared for equality |
| `headgate._auth` | `CachedTokenAuth.attach #3` | header-name case |
| `headgate._auth` | `CachedTokenAuth.attach #4` | header-name case |
| `headgate.pagination._link` | `parse_link_header #4` | at `i == end` the skip that follows breaks the loop at once |
| `headgate.pagination._link` | `next_link #3` | header-name case |
| `headgate.breaker._breaker` | `CircuitBreaker._settle_probe #2` | `_probe_in_flight` is only tested for truth |
| `headgate.cache._credentials` | `CredentialScope.__init__ #7` | the identities only need one order per configuration: the auth's after the first middleware's still tells every configuration apart, since a client's auth is never one of its middleware |
| `headgate.cache._credentials` | `CredentialScope.__init__ #24` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.__init__ #25` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #7` | codec names are case-insensitive |
| `headgate.cache._credentials` | `CredentialScope.view #13` | a header's digest is stored only as that header's value and a label only as the URL's fragment, so the two never need telling apart |
| `headgate.cache._credentials` | `CredentialScope.view #16` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #17` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #20` | codec names are case-insensitive |
| `headgate.cache._credentials` | `CredentialScope.view #23` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #24` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #27` | codec names are case-insensitive |
| `headgate.cache._credentials` | `CredentialScope.view #30` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #31` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #34` | codec names are case-insensitive |
| `headgate.cache._credentials` | `CredentialScope.view #46` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #47` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #49` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #50` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #52` | `copy_with(userinfo=None)` clears userinfo exactly as `b""` |
| `headgate.cache._credentials` | `_digest #10` | `int.to_bytes` defaults to big-endian |
| `headgate.cache._credentials` | `_digest #11` | a 9-byte length prefix is as unambiguous as an 8-byte one |
| `headgate.cache._transport` | `_guarded_chunks #1` | the message never surfaces: the error becomes `CacheStorageError("TypeError")` and the transport falls back |
| `headgate.cache._transport` | `_guarded_chunks #2` | the message never surfaces: the error becomes `CacheStorageError("TypeError")` and the transport falls back |
| `headgate.cache._transport` | `_guarded_chunks #3` | the message never surfaces: the error becomes `CacheStorageError("TypeError")` and the transport falls back |
| `headgate.cache._transport` | `_guarded_chunks #4` | the message never surfaces: the error becomes `CacheStorageError("TypeError")` and the transport falls back |
| `headgate.cache._transport` | `_Store.__init__ #5` | `_written` is only tested for truth |
| `headgate.cache._transport` | `_Store.__call__ #14` | the id is random either way: hishel asks for none, so the deferred entry's is a fresh UUID, as the storage draws without one, and nothing reads it |
| `headgate.cache._transport` | `_Store.__call__ #18` | the id is random either way: hishel asks for none, so the deferred entry's is a fresh UUID, as the storage draws without one, and nothing reads it |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch #13` | `copy_with(userinfo=None)` clears userinfo exactly as `b""` |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch #24` | httpx2's `aiter_raw` closes the response on every exit, so the explicit close finds nothing open |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch_cached #17` | header-name case |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch_cached #27` | once hishel returns, only reading its response can raise, and httpx2's `aiter_raw` closes the response on every exit |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch_cached #28` | once hishel returns, only reading its response can raise, and httpx2's `aiter_raw` closes the response on every exit |
| `headgate.cache._policy` | `filter_policy_of #6` | `MethodFilter` upper-cases the methods it is given |
| `headgate.cache._conditional` | `Validator.of #4` | header-name case |
| `headgate.cache._conditional` | `Validator.of #5` | header-name case |
| `headgate.cache._conditional` | `Validator.of #9` | header-name case |
| `headgate.cache._conditional` | `Validator.of #10` | header-name case |
| `headgate.retry._transport` | `_Sequence._hedged #13` | `keep` is compared by identity, and no attempt is `""` any more than `None` |
| `headgate.retry._transport` | `_first_answer #5` | a finished send the `break` skips stays in `pending`, so the next `asyncio.wait` hands it back at once |
| `headgate.retry._classify` | `_should_retry #9` | header-name case |
| `headgate.retry._classify` | `_should_retry #10` | a missing header read as `xxxx` is neither `true` nor `false`, like `""` |
| `headgate._core._paths` | `path_has_fragment #13` | the extra start positions give only slices shorter than the fragment, which never equal it |
| `headgate._core._paths` | `path_has_fragment #14` | the extra start position gives only a slice shorter than the fragment, which never equals it |
| `headgate._core._events` | `Emitter.emit #1` | `delivered` is only tested for truth |
| `headgate._core._retry_after` | `retry_after_of #9` | header-name case |
| `headgate._core._retry_after` | `retry_after_of #10` | a missing header read as `XXXX` is not a number, like `""` |
| `headgate._core._retry_after` | `retry_after_of #16` | header-name case |
| `headgate.security._transport` | `address_hook #5` | codec names are case-insensitive |
| `headgate.security._addresses` | `resolve_with_loop #3` | the port only fills the socket address's port, which is dropped |
| `headgate.security._addresses` | `resolve_with_loop #7` | the extra socket types repeat addresses, which are de-duplicated |
| `headgate.security._downgrade` | `refuse_https_downgrade #12` | header-name case |
| `headgate.security._downgrade` | `refuse_https_downgrade #13` | header-name case |
| `headgate.security._downgrade` | `refuse_https_downgrade #18` | header-name case |
| `headgate.security._downgrade` | `refuse_https_downgrade #19` | header-name case |
| `headgate.streaming._stream` | `OpenStream.aiter_events #10` | header-name case |
| `headgate.streaming._events` | `EventParser.__init__ #3` | `_after_cr` is only tested for truth |
| `headgate.streaming._events` | `EventParser.__init__ #9` | an event's type is read only as `type or "message"`, where `None` is as falsy as `""` |
| `headgate.streaming._events` | `EventParser.feed #4` | `_after_cr` is only tested for truth |
| `headgate.streaming._events` | `EventParser._end_line #3` | `_first_line` is only tested for truth |
| `headgate.streaming._events` | `EventParser._end_line #24` | codec names are case-insensitive |
| `headgate.testing._transport` | `_path #7` | codec names are case-insensitive |
| `headgate.testing._codec` | `check_codec #27` | the planted key's value only has to be a string where an int is declared, which this one still is |
| `headgate.testing._codec` | `check_codec #28` | the planted key's value only has to be a string where an int is declared, which this one still is |
| `headgate.testing._codec` | `check_codec #57` | a third of the body is cut short as surely as half of it; the check needs only a cut that loses more than a final delimiter |
| `headgate.testing._codec` | `check_codec #67` | the added field only has to be one the record does not declare, which the renamed one still is |
| `headgate.testing._codec` | `check_codec #69` | the added field's value is never read |
| `headgate.testing._codec` | `check_codec #70` | the added field's value is never read |
| `headgate.testing._time` | `TestClock.__init__ #6` | `_driven_on` is only compared by identity with a loop, which `""` is not any more than `None` |
| `headgate.testing._time` | `TestClock.__init__ #7` | `_drive` resets the count before any turn reads it |
| `headgate.testing._time` | `TestClock.__init__ #8` | `_drive` resets the count before any turn reads it |
| `headgate.testing._time` | `TestClock._turn #11` | `_driven_on` is only compared by identity with a loop, which `""` is not any more than `None` |
| `headgate.testing.faults` | `_read_timeout #12` | the only non-number a read timeout can be is `None`, which the fallback also returns |
| `headgate.testing.faults` | `FaultyTransport._take #48` | `body` is only tested for truth |
| `headgate.testing.faults` | `_write #14` | a write cut at 0 bytes is only asked whether it completed; its count is never read |
| `headgate.testing.faults` | `_write #15` | `complete` is only tested for truth |
| `headgate.testing.faults` | `_write #16` | a write cut at 0 bytes is only asked whether it completed; its count is never read |
| `headgate.testing.faults` | `_write #18` | a write cut at 0 bytes is only asked whether it completed; its count is never read |
| `headgate.testing.faults` | `_write #54` | `complete` is only tested for truth |
| `headgate.testing.faults` | `_write #57` | each fault call is written once, so `=` and `+=` agree |
| `headgate.ratelimit._feedback` | `_ietf_quota #9` | header-name case |
| `headgate.ratelimit._feedback` | `_ietf_quota #10` | header-name case |
| `headgate.ratelimit._feedback` | `_policy_units #9` | header-name case |
| `headgate.ratelimit._feedback` | `_policy_units #10` | header-name case |
| `headgate.ratelimit._feedback` | `_policy_units #11` | a missing field read as the token `XXXX` names a policy no item refers to |
| `headgate.ratelimit._feedback` | `_de_facto_quota #8` | header-name case |
| `headgate.ratelimit._feedback` | `_de_facto_quota #9` | header-name case |
| `headgate.ratelimit._feedback` | `_de_facto_quota #10` | a missing header read as `XXXX` holds no number, like `""` |
| `headgate.ratelimit._feedback` | `_de_facto_quota #24` | header-name case |
| `headgate.ratelimit._feedback` | `_de_facto_quota #25` | header-name case |
| `headgate.ratelimit._feedback` | `_de_facto_quota #26` | a missing header read as `XXXX` holds no number, like `""` |
| `headgate.ratelimit._budget` | `_Turn.__init__ #6` | `slept` is only compared with a (shift, pause end) pair, which `""` is not any more than `None` |
