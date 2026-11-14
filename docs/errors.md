# Errors

Every error class has one module to import it from:

| Error | Import it from |
| --- | --- |
| `ClientError` | `headgate` |
| `TransportError` | `headgate` |
| `ClientTimeoutError` | `headgate` |
| `CircuitOpenError` | `headgate.errors` |
| `UpstreamStatusError` | `headgate` |
| `AuthError` | `headgate` |
| `RateLimitError` | `headgate` |
| `ResponseParseError` | `headgate.errors` |
| `ResponseTooLargeError` | `headgate.errors` |
| `ForbiddenDestinationError` | `headgate.errors` |
| `PageLimitError` | `headgate.errors` |
| `ClientConfigurationError` | `headgate.errors` |
| `DecodeError` | `headgate.errors` |
| `RefusedError` | `headgate.errors` |

The six a caller catches most are at the top level; the rest live in `headgate.errors`. `headgate.errors` also defines
the six, so `from headgate.errors import ClientError` still works, but type checkers and these docs name
`headgate.ClientError`.

Every failure a client raises is a `headgate.ClientError`, and the shape of the tree is part of the contract:
an application boundary can map it onto its own answers without knowing which client failed.

```text
ClientError                      also: an unparsable URL, a pagination loop
├── TransportError               a network failure that outlasted retries
│   ├── ClientTimeoutError       a timeout, or the call's deadline
│   └── CircuitOpenError         the origin's breaker refused the call unsent
├── UpstreamStatusError          an undeclared status; a 5xx that outlasted retries; a refusal in a body
│   ├── AuthError                401 or 403: our credential was refused
│   └── RateLimitError           429, a quota refusal, or a server pause past the deadline
├── ResponseParseError           a 2xx body the reader could not read
├── ResponseTooLargeError        a body past max_response_bytes
├── ForbiddenDestinationError    a hop the SSRF guard refused, or a token mint sent off its origin
├── PageLimitError               a paginated walk spent its page budget with pages left
└── ClientConfigurationError     our own setup is wrong
```

A natural mapping for a service calling upstreams: `ClientTimeoutError` to 504, `ClientConfigurationError` to 503, and
every other `ClientError` to 502. `DecodeError` is not a `ClientError`: a codec raises it, and the client turns it
into `ResponseParseError` before a caller sees it. Neither is `headgate.errors.RefusedError`, which a reader (or an
`HttpOptions(response_check=)`) produces for a refusal: the client converts it into the `ClientError` its `kind`
names.

## What every error carries

| Attribute | Meaning |
| --- | --- |
| `source` | the host of the client's `base_url`; it also prefixes the message, `[api.catalog.example] ...` |
| `method`, `url` | the request line of the call, the URL as the call gave it, with credentials redacted and cut to 300 characters |
| `request_id` | the upstream's request id, from the first of `HttpOptions.request_id_headers` the response carried |
| `attempts` | how many sends the call made, retries and followed redirects included |
| `retryable` | whether sending the call again later could succeed and is safe as it stands: for a status or a network failure, the call's `RetryPolicy` verdict, and `True` for the refusals headgate makes before sending (an open breaker, a rate budget past the deadline) (see [retries](retries.md#what-retryable-means)) |
| `retry_after` | the seconds the upstream (or an open breaker, or a paused budget) asked to wait, or `None` |
| `sent` | whether any byte of the call can have reached the network, by any send, retry or followed redirect: `False` only when none can have, so the call is safe to send again whatever its method; `True` when some may have, so a write may have been processed; `None` on an error no client call raised. A token mint's own requests never count |
| notes | one line per attempt, and the reason the retries stopped (see [retries](retries.md#attempt-history)) |

An error your own code raises takes these attributes, the notes aside, as optional keywords:
`ClientError(message, **details)`, where `details` is the TypedDict `ErrorDetails`.

`request_id` is read from `x-request-id`, `request-id`, `x-amzn-requestid`, `x-ms-request-id` and `cf-ray` by default;
quote it when you contact the provider.

`UpstreamStatusError` (and `AuthError`, `RateLimitError`) adds:

- `status_code`: the status the wire sent, never invented. A refusal written in a 200 body carries 200, and the rate
  budget's own refusal, which no response answered, carries `None`, so code that orders it (`>=`) narrows it first;
- `code`: the provider's own code, where a reader's `RefusedError` named one;
- `problem`: the RFC 9457 problem detail, when the body was `application/problem+json` (`type`, `title`, `status`,
  `detail`, `instance`, and `extensions` read-only at any depth). The message then quotes its title and detail;
- otherwise the message ends with a bounded, single-line snippet of the body, which is usually the diagnosis.

`ResponseParseError` adds `status_code` and `content_type`, and its message names the reader's diagnosis, bounded. A
codec's reader never quotes an input value or key there; a hand-written reader's own exception text (a `ValueError`
from `int(field)`, say) is passed through as it is worded, only shortened.

Every `ClientError` pickles with its attributes, so it can cross a process boundary or be stored by a durable workflow
engine.

## Reading a failure

```python
from headgate import ClientError, UpstreamStatusError

try:
    series = await catalog.get_series("GDP")
except ClientError as exc:
    if not exc.retryable:
        if isinstance(exc, UpstreamStatusError) and exc.problem is not None:
            report(exc.problem.title, exc.problem.detail)
        raise
    reschedule(after=exc.retry_after)
```

`retryable` covers every error the same way, so one check reschedules a surviving 503 on a GET, a 429, an open
breaker and a rate budget's own refusal, each after its `retry_after`. A write that is not retryable re-raises, and its
`sent` says whether it may have reached the server, which is what decides whether to reconcile before trying again.

## Messages are safe to show

A message can reach an API response or a log, so every part that comes from outside is bounded and sanitized: URLs
have userinfo and secret query values redacted, exception messages and decoder diagnoses are cut to 1,000 characters,
body snippets to 300, and a header value the HTTP library refused to encode is named by its header, never its value.
A snippet is read in the body's `charset`, or as UTF-8 when it declares none or one that is no text encoding
(`base64`, say). A decoding library's error is never chained to a `DecodeError`, since it may quote the body.
A failed token mint is reported against the token endpoint, never the API URL that needed the token.
