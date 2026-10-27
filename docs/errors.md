# Errors

Every failure a client raises is a `headgate.errors.ClientError`, and the shape of the tree is part of the contract:
an application boundary can map it onto its own answers without knowing which client failed.

```text
ClientError                      also: an unparseable URL, a pagination loop
├── TransportError               a network failure that outlasted retries
│   ├── ClientTimeoutError       a timeout, or the call's deadline
│   └── CircuitOpenError         the origin's breaker refused the call unsent
├── UpstreamStatusError          an undeclared status; a 5xx that outlasted retries; a refusal in a body
│   ├── AuthError                401 or 403: our credential was refused
│   └── RateLimitError           429, a quota refusal, or a server pause past the deadline
├── ResponseParseError           a 2xx body the reader could not read
├── ResponseTooLargeError        a body past max_response_bytes
├── ForbiddenDestinationError    a hop the SSRF guard refused
├── PageLimitError               a paginated walk spent its page budget with pages left
└── ClientConfigurationError     our own setup is wrong
```

A natural mapping for a service calling upstreams: `ClientTimeoutError` to 504, `ClientConfigurationError` to 503, and
every other `ClientError` to 502. `DecodeError` is not a `ClientError`: a codec raises it, and the client turns it
into `ResponseParseError` before a caller sees it.

## What every error carries

| Attribute | Meaning |
| --- | --- |
| `source` | the client class's name; it also prefixes the message, `[CatalogClient] ...` |
| `method`, `url` | the request line of the call, the URL as the call gave it, with credentials redacted and cut to 300 characters |
| `request_id` | the upstream's request id, from the first of `HttpOptions.request_id_headers` the response carried |
| `attempts` | how many sends the call made, retries and followed redirects included |
| `retryable` | whether the call's `RetryPolicy` would re-send after this outcome: a status in its `statuses` or a network failure, and only when sending again is safe for the method (see [retries](retries.md#what-retryable-means)) |
| `retry_after` | the seconds the upstream (or an open breaker, or a paused budget) asked to wait, or `None` |
| notes | one line per attempt, and the reason the retries stopped (see [retries](retries.md#attempt-history)) |

`request_id` is read from `x-request-id`, `request-id`, `x-amzn-requestid`, `x-ms-request-id` and `cf-ray` by default;
quote it when you contact the provider.

`UpstreamStatusError` (and `AuthError`, `RateLimitError`) adds:

- `status_code`: the status the wire sent, never invented. A refusal written in a 200 body carries 200;
- `code`: the provider's own code, where a reader's `RefusedError` named one;
- `problem`: the RFC 9457 problem detail, when the body was `application/problem+json` (`type`, `title`, `status`,
  `detail`, `instance`, and `extensions` read-only at any depth). The message then quotes its title and detail;
- otherwise the message ends with a bounded, single-line snippet of the body, which is usually the diagnosis.

`ResponseParseError` adds `status_code` and `content_type`, and its message names the reader's diagnosis, bounded,
never quoting an input value.

Every `ClientError` pickles with its attributes, so it can cross a process boundary or be stored by a durable workflow
engine.

## Reading a failure

```python
from headgate import ClientError, RateLimitError, UpstreamStatusError

try:
    series = await catalog.get_series("GDP")
except RateLimitError as exc:
    reschedule(after=exc.retry_after)
except UpstreamStatusError as exc:
    if exc.problem is not None:
        report(exc.problem.title, exc.problem.detail)
    raise
except ClientError as exc:
    if exc.retryable:
        reschedule(after=exc.retry_after)
    else:
        raise
```

## Messages are safe to show

A message can reach an API response or a log, so every part that comes from outside is bounded and sanitized: URLs
have userinfo and secret query values redacted, exception messages and decoder diagnoses are cut to 1,000 characters,
body snippets to 300, and a header value the HTTP library refused to encode is named by its header, never its value.
A failed token mint is reported against the token endpoint, never the API URL that needed the token.
