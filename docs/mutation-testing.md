# Mutation testing

Coverage says a line ran; mutation testing says a test would notice if it were wrong. [mutmut](https://mutmut.readthedocs.io/)
rewrites `src/` one small change at a time (a flipped comparison, a dropped argument, a changed string) and runs the
suite against each rewrite, a *mutant*. A mutant the suite fails on is killed; one it passes survives, and is either a
missing test or a change no input can tell apart from the original (an *equivalent* mutant).

## Running it

The configuration is `[tool.mutmut]` in `pyproject.toml`. Mutation runs leave out the tests of the source's own shape
(the API-surface snapshot, the import boundary, the no-logging contract), which read the rewritten files.

```sh
uv run mutmut run                       # every mutant, about 15 minutes on 10 workers
uv run mutmut run "headgate._failures*" # one module's mutants, while working on it
uv run mutmut results                   # the survivors
uv run mutmut show <name>               # one mutant's diff
```

Delete `mutants/` if a run gets confused after files move. `--max-children` sets the number of workers.

On macOS, mutmut reports some mutants as `segfault`: the mutant made a test take a path through the system TLS stack
(an SSL context built without verification, say) in a forked worker, where Security.framework aborts the process. The
same tests fail on those mutants when run in a plain process, so they are counted as killed.

## Latest score

A full run on 2026-10-25 (mutmut 3.8, 10 workers, about 17 minutes) generated 5,573 mutants. It ran against the source
of headgate 0.2.0 ([`cab9a27`](https://github.com/kelsayed17/headgate/tree/cab9a27/src/headgate)), so the modules and
functions named below are that source's: some have since been renamed, merged or made public.

| Outcome | Mutants |
| --- | --- |
| killed by a failing test | 5,261 |
| killed by a crash in the forked worker (`segfault`, see above) | 128 |
| killed by a timeout | 69 |
| survived, every one equivalent (listed below) | 115 |

That is 5,458 of 5,573 detected (97.9%), and every mutant that changes behaviour is detected. Two of the killed are
counted from a re-run of those two mutants after the full run, against tests written for its last non-equivalent
survivors (both in `headgate.testing.faults`).

## Equivalent mutants

Every survivor was checked, and each one left is equivalent: no input distinguishes them. Most fall in a
few families:

- **header-name case**: httpx2 headers are case-insensitive, so `headers.get("ETag")` and `headers.get("etag")` read
  the same field;
- **codec and distribution names**: `"ascii"`/`"ASCII"`, `"latin-1"`/`"LATIN-1"`, `"utf-8"`/`"UTF-8"`, and
  `metadata.version("HEADGATE")` (names are normalised);
- **falsy stand-ins**: `None` for `False` or `""` where the value is only tested for truth;
- **defaults spelt out**: an argument equal to the callee's default (`to_bytes(8, "big")`, `zip(..., strict=False)`,
  `secrets.token_bytes(32)`, `copy_with(userinfo=None)`, which clears userinfo exactly as `b""` does);
- **unreachable fallbacks**: a default for a value the code never sees missing (a header read with `""` or
  `"XXXX"` when absent, both of which parse to nothing).

| Module | Mutant | Why it is equivalent |
| --- | --- | --- |
| `headgate._auth` | `read_hop #13` | the status of the response built only to read the hop's body is never read |
| `headgate._auth` | `read_hop #18` | the status of the response built only to decode the hop's body is never read |
| `headgate._auth` | `CachedTokenAuth.__init__ #3` | `token_bytes(None)` draws the default 32 bytes |
| `headgate._auth` | `CachedTokenAuth.__init__ #4` | a 33-byte random identity is as unique as a 32-byte one; it is only compared for equality |
| `headgate._auth` | `CachedTokenAuth._attach #3` | header-name case |
| `headgate._auth` | `CachedTokenAuth._attach #4` | header-name case |
| `headgate._body` | `read_capped #4` | header-name case |
| `headgate._client` | `AsyncBaseClient.with_options #11` | `_owns_pool` is only tested for truth |
| `headgate._client` | `AsyncBaseClient.stream #14` | header-name case |
| `headgate._client` | `AsyncBaseClient.stream #15` | header-name case |
| `headgate._client` | `AsyncBaseClient._dispatch #27` | header-name case |
| `headgate._client` | `query_value #2` | httpx2 sends any other value as its `str()`, exactly what the function returns; it exists for the static type |
| `headgate._client` | `stamp_accept #9` | header-name case |
| `headgate._client` | `stamp_accept #10` | header-name case |
| `headgate._core._events` | `Emitter.emit #1` | `delivered` is only tested for truth |
| `headgate._core._retry_after` | `retry_after_of #9` | header-name case |
| `headgate._core._retry_after` | `retry_after_of #10` | a missing header read as `XXXX` is not a number, like `""` |
| `headgate._core._retry_after` | `retry_after_of #16` | header-name case |
| `headgate._curl` | `httpx_response #6` | httpx2 headers' `in` is case-insensitive |
| `headgate._curl` | `httpx_response #31` | codec names are case-insensitive |
| `headgate._curl` | `httpx_response #44` | httpx2 attaches the request to an error raised while it reads a body |
| `headgate._curl` | `_CurlByteStream.__init__ #1` | httpx2 attaches the request to an error raised while it reads a body |
| `headgate._deprecation` | `deprecation_of #4` | header-name case |
| `headgate._deprecation` | `deprecation_of #5` | header-name case |
| `headgate._deprecation` | `deprecation_of #9` | header-name case |
| `headgate._deprecation` | `deprecation_of #10` | header-name case |
| `headgate._failures` | `CallSite._request_id #3` | a missing header read as `None` is as falsy as `""` |
| `headgate._failures` | `CallSite._request_id #5` | a missing header read as `None` is as falsy as `""` |
| `headgate._failures` | `body_snippet #7` | httpx2's `Response.encoding` is never falsy (it falls back to utf-8 itself) |
| `headgate._failures` | `body_snippet #8` | httpx2's `Response.encoding` is never falsy (it falls back to utf-8 itself) |
| `headgate._failures` | `problem_of #10` | header-name case |
| `headgate._failures` | `problem_of #11` | a missing content-type read as `xxxx` is not a problem document, like `""` |
| `headgate._failures` | `parse_error #15` | header-name case |
| `headgate._failures` | `mint_error #10` | an empty body snippet is treated as absent, exactly like `None` |
| `headgate._redirects` | `refuse_unreplayable_redirect #15` | header-name case |
| `headgate._redirects` | `refuse_unreplayable_redirect #16` | header-name case |
| `headgate._responses` | `Expect.accepts #12` | header-name case |
| `headgate._responses` | `Expect.accepts #13` | a missing content-type read as `xxxx` is no media type, like `""` |
| `headgate._responses` | `event_stream #2` | the status of the response built only to frame the body is never read |
| `headgate._responses` | `event_stream #10` | the status of the response built only to frame the body is never read |
| `headgate._responses` | `event_stream #12` | header-name case |
| `headgate._responses` | `event_stream #14` | httpx2's EventSource compares the media type case-insensitively |
| `headgate._responses` | `event_stream #19` | the method of the request built only to frame the body is never read |
| `headgate._responses` | `event_stream #20` | the method of the request built only to frame the body is never read |
| `headgate._user_agent` | `headgate_version #3` | distribution names are normalised, so `HEADGATE` finds `headgate` |
| `headgate.breaker._breaker` | `CircuitBreaker._settle_probe #2` | `_probe_in_flight` is only tested for truth |
| `headgate.cache._conditional` | `Validator.of #4` | header-name case |
| `headgate.cache._conditional` | `Validator.of #5` | header-name case |
| `headgate.cache._conditional` | `Validator.of #9` | header-name case |
| `headgate.cache._conditional` | `Validator.of #10` | header-name case |
| `headgate.cache._credentials` | `CredentialScope.view #7` | codec names are case-insensitive |
| `headgate.cache._credentials` | `CredentialScope.view #12` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #13` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #16` | codec names are case-insensitive |
| `headgate.cache._credentials` | `CredentialScope.view #27` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #28` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #30` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #31` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope.view #33` | `copy_with(userinfo=None)` clears userinfo exactly as `b""` |
| `headgate.cache._credentials` | `CredentialScope._label #8` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope._label #9` | the part tags stay distinct from one another, so the length-prefixed MAC input still separates every source |
| `headgate.cache._credentials` | `CredentialScope._label #22` | `int.to_bytes` defaults to big-endian |
| `headgate.cache._credentials` | `CredentialScope._label #23` | a 9-byte length prefix is as unambiguous as an 8-byte one |
| `headgate.cache._transport` | `guarded_chunks #2` | the message never surfaces: the error becomes `CacheStorageError("TypeError")` and the transport falls back |
| `headgate.cache._transport` | `guarded_chunks #3` | the message never surfaces: the error becomes `CacheStorageError("TypeError")` and the transport falls back |
| `headgate.cache._transport` | `guarded_chunks #4` | the message never surfaces: the error becomes `CacheStorageError("TypeError")` and the transport falls back |
| `headgate.cache._transport` | `guarded_chunks #5` | the message never surfaces: the error becomes `CacheStorageError("TypeError")` and the transport falls back |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch #13` | `copy_with(userinfo=None)` clears userinfo exactly as `b""` |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch #24` | httpx2's `aiter_raw` closes the response on every exit, so the explicit close finds nothing open |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch_cached #17` | header-name case |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch_cached #26` | httpx2's `aiter_raw` closes the response on every exit, so the explicit close finds nothing open |
| `headgate.cache._transport` | `ResilientCacheTransport._fetch_cached #27` | httpx2's `aiter_raw` closes the response on every exit, so the explicit close finds nothing open |
| `headgate.errors` | `Problem.__setstate__ #3` | `strict=None` is as false as `strict=False`, the default |
| `headgate.errors` | `Problem.__setstate__ #6` | `strict=False` is `zip`'s default |
| `headgate.health` | `breaker_states #12` | two snapshots of equal rank are equal (state and refusing are all a snapshot holds) |
| `headgate.otel` | `OpenTelemetryObserver.__init__ #26` | the OpenTelemetry SDK lower-cases instrument names |
| `headgate.pagination._link` | `parse_link_header #4` | at `i == end` the skip that follows breaks the loop at once |
| `headgate.pagination._link` | `next_link #3` | header-name case |
| `headgate.pydantic` | `adapter #1` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `adapter #5` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `adapter #6` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `adapter #7` | `typing.cast` ignores its type argument at runtime |
| `headgate.pydantic` | `describe #5` | the message reads only each error's `loc` and `msg`, which these flags do not change |
| `headgate.pydantic` | `describe #6` | the message reads only each error's `loc` and `msg`, which these flags do not change |
| `headgate.pydantic` | `describe #7` | the message reads only each error's `loc` and `msg`, which these flags do not change |
| `headgate.pydantic` | `describe #8` | the message reads only each error's `loc` and `msg`, which these flags do not change |
| `headgate.pydantic` | `describe #9` | the message reads only each error's `loc` and `msg`, which these flags do not change |
| `headgate.pydantic` | `describe #10` | the message reads only each error's `loc` and `msg`, which these flags do not change |
| `headgate.pydantic` | `to_builtins #6` | pydantic-core's `to_jsonable_python` serialises a model by alias already |
| `headgate.ratelimit._feedback` | `ietf_quota #9` | header-name case |
| `headgate.ratelimit._feedback` | `ietf_quota #10` | header-name case |
| `headgate.ratelimit._feedback` | `policy_units #9` | header-name case |
| `headgate.ratelimit._feedback` | `policy_units #10` | header-name case |
| `headgate.ratelimit._feedback` | `policy_units #11` | a missing field read as the token `XXXX` names a policy no item refers to |
| `headgate.ratelimit._feedback` | `de_facto_quota #8` | header-name case |
| `headgate.ratelimit._feedback` | `de_facto_quota #9` | header-name case |
| `headgate.ratelimit._feedback` | `de_facto_quota #10` | a missing header read as `XXXX` holds no number, like `""` |
| `headgate.ratelimit._feedback` | `de_facto_quota #24` | header-name case |
| `headgate.ratelimit._feedback` | `de_facto_quota #25` | header-name case |
| `headgate.ratelimit._feedback` | `de_facto_quota #26` | a missing header read as `XXXX` holds no number, like `""` |
| `headgate.retry._classify` | `should_retry #9` | header-name case |
| `headgate.retry._classify` | `should_retry #10` | a missing header read as `xxxx` is neither `true` nor `false`, like `""` |
| `headgate.retry._transport` | `_Sequence._hedged #9` | `failed` is read only once every send has finished, by which time a failure has set it |
| `headgate.retry._transport` | `_Sequence._hedged #10` | `keep` is compared by identity, and no attempt is `""` any more than `None` |
| `headgate.security._addresses` | `resolve_with_loop #3` | the port only fills the socket address's port, which is dropped |
| `headgate.security._addresses` | `resolve_with_loop #7` | the extra socket types repeat addresses, which are de-duplicated |
| `headgate.security._downgrade` | `refuse_https_downgrade #12` | header-name case |
| `headgate.security._downgrade` | `refuse_https_downgrade #13` | header-name case |
| `headgate.security._downgrade` | `refuse_https_downgrade #18` | header-name case |
| `headgate.security._downgrade` | `refuse_https_downgrade #19` | header-name case |
| `headgate.security._transport` | `address_hook #5` | codec names are case-insensitive |
| `headgate.streaming._stream` | `Stream.aiter_events #10` | header-name case |
| `headgate.testing._transport` | `path #7` | codec names are case-insensitive |
| `headgate.testing.faults` | `read_timeout #12` | the only non-number a read timeout can be is `None`, which the fallback also returns |
| `headgate.testing.faults` | `write #49` | each fault call is written once, so `=` and `+=` agree |
