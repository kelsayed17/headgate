---
title: Standards
---

# Built on the standards

HTTP already has answers to the hard questions an API client faces: which requests are safe to send twice, how long to
wait when a server says "slow down", what an error body looks like, where the next page is. They are written down in
RFCs. headgate reads them so you do not have to, and follows them by default.

<div class="grid cards" markdown>

-   :lucide-badge-check:{ .lg .hg-icon } **17 specifications**

    ---

    IETF RFCs and drafts, the WHATWG event-stream format and Python's own PEP 750, each tied to the behavior it
    drives.

-   :lucide-handshake:{ .lg .hg-icon } **Polite by default**

    ---

    `Retry-After` waited exactly, rate-limit headers obeyed, `Idempotency-Key` sent on writes. Providers see a
    well-behaved client.

-   :lucide-shield-check:{ .lg .hg-icon } **Strict where it matters**

    ---

    A malformed header is ignored, as each spec asks, rather than guessed at. Nothing unsafe is retried.

-   :lucide-scale:{ .lg .hg-icon } **Honest about exceptions**

    ---

    The one place headgate chooses differently, the cache, is [spelled out](#caching-where-headgate-chooses-differently)
    with the reason.

</div>

!!! tip "You do not need to know any of this to use headgate"

    Every row below is something headgate does for you. The page is here for the curious, for reviewers, and for
    anyone who has to justify a dependency.

## Sending and retrying safely

| Standard | What it says | What headgate does |
| --- | --- | --- |
| :lucide-repeat: [RFC 9110 §9.2.2](https://www.rfc-editor.org/rfc/rfc9110#section-9.2.2) Idempotent methods | GET, HEAD, OPTIONS, PUT and DELETE can be repeated without extra effect | only these are [retried](retries.md#what-is-re-sent) after an ambiguous failure, and only these can be [hedged](retries.md#hedging) |
| :lucide-key-round: [Idempotency-Key](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) (IETF draft) | a client key lets a server recognize a re-sent POST or PATCH | [`IdempotencyKeys()`](retries.md#idempotency-keys) stamps one key per call, so every retry carries the same one |
| :lucide-clock-alert: [RFC 8470](https://www.rfc-editor.org/rfc/rfc8470) 425 Too Early | the server did not process the request | retried for every method, POST included, since nothing happened |
| :lucide-git-pull-request-closed: [RFC 9113 §8.7](https://www.rfc-editor.org/rfc/rfc9113#section-8.7) HTTP/2 `REFUSED_STREAM` | the server refused the stream before doing any work | retried for every method, for the same reason |
| :lucide-corner-down-right: [RFC 9110 §15.4](https://www.rfc-editor.org/rfc/rfc9110#section-15.4) Redirects | 303 becomes GET; 307 and 308 keep the method and body | followed as written, except a 307 or 308 that would [re-send a one-shot body](security.md#redirects) and any https-to-http hop |

## Waiting when asked

| Standard | What it says | What headgate does |
| --- | --- | --- |
| :lucide-hourglass: [RFC 9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110#section-10.2.3) `Retry-After` | how long to wait before trying again, in seconds or as a date | [waited exactly](retries.md#retry-after) on a 429 or 503, up to a cap; past it the call fails at once and tells you when to come back |
| :lucide-octagon-pause: [RFC 6585 §4](https://www.rfc-editor.org/rfc/rfc6585#section-4) 429 Too Many Requests | the client is over its rate limit | raised as `RateLimitError`, and [pauses the shared budget](rate-budgets.md#server-feedback) so other calls stop too |
| :lucide-gauge: [RateLimit headers](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/) (IETF draft) | `RateLimit` and `RateLimit-Policy` say how much quota is left and when it returns | an exhausted quota pauses the [rate budget](rate-budgets.md#server-feedback) until it returns; other quota units are skipped |
| :lucide-braces: [RFC 9651](https://www.rfc-editor.org/rfc/rfc9651) Structured Field Values | the syntax the RateLimit headers are written in | parsed with the RFC's own limits; a malformed field is ignored, as the draft asks |

## Reading responses

| Standard | What it says | What headgate does |
| --- | --- | --- |
| :lucide-file-warning: [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) Problem Details | errors as `application/problem+json` with `type`, `title`, `detail` | parsed into [`exc.problem`](errors.md#what-every-error-carries); the message shows its title and detail instead of raw JSON |
| :lucide-file-type: [RFC 6839](https://www.rfc-editor.org/rfc/rfc6839) Structured syntax suffixes | `application/problem+json` is a kind of JSON | an `Expect(accept="application/json")` [accepts it](responses.md#content-negotiation) |
| :lucide-refresh-ccw: [RFC 9110 §13.1](https://www.rfc-editor.org/rfc/rfc9110#section-13.1) Conditional requests | `If-None-Match` and `If-Modified-Since` let a server answer 304 Not Modified | [`conditional()`](caching.md#conditional-reads) sends them and returns `Fresh` or `NotModified` |
| :lucide-radio: [WHATWG Server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html) | the `text/event-stream` format and `Last-Event-ID` resumption | [`aiter_events()`](streaming.md#server-sent-events) parses events as they arrive; `last_event_id=` resumes |
| :lucide-calendar-x: [RFC 9745](https://www.rfc-editor.org/rfc/rfc9745) `Deprecation` and [RFC 8594](https://www.rfc-editor.org/rfc/rfc8594) `Sunset` | an endpoint announces it is deprecated or going away | a [`Deprecated` event](observability.md#deprecation-and-sunset) with the dates and links, once per call |

## Pages and links

| Standard | What it says | What headgate does |
| --- | --- | --- |
| :lucide-link: [RFC 8288](https://www.rfc-editor.org/rfc/rfc8288) Web Linking | `Link: <...>; rel="next"` names the next page | [`LinkHeader`](pagination.md#strategies) follows it; the parser handles quoted commas and repeated parameters as the RFC says |
| :lucide-globe-lock: [RFC 6454](https://www.rfc-editor.org/rfc/rfc6454) Origins | an origin is a scheme, host and port | credentials are [pinned to an origin](security.md#credentials-stay-on-your-origin), circuit breakers are kept per origin |

## Addresses and paths

| Standard | What it says | What headgate does |
| --- | --- | --- |
| :lucide-network: [RFC 6052](https://www.rfc-editor.org/rfc/rfc6052) NAT64 | `64:ff9b::/96` carries an IPv4 address inside IPv6 | the [SSRF guard](security.md#the-ssrf-guard) judges such an address by the IPv4 address it reaches |
| :lucide-code: [PEP 750](https://peps.python.org/pep-0750/) Template strings | Python 3.14's `t"..."` keeps values apart from the template | each value in a [path](security.md#paths-built-from-values) is percent-encoded as exactly one segment |

## Caching: where headgate chooses differently

[RFC 9111](https://www.rfc-editor.org/rfc/rfc9111) says what a shared HTTP cache may store, based on headers such as
`Cache-Control`. headgate's [cache](caching.md) is hishel's, which implements RFC 9111, but headgate configures it with
**your rules** (which paths, methods and statuses, for how long) instead of the provider's headers. That is on
purpose: many APIs send no cache headers, or `no-store` on data that is safe to keep for an hour. When you want the
origin to decide, use [conditional reads](caching.md#conditional-reads), which follow RFC 9110 to the letter.

## Widely used conventions

Not every useful convention is an RFC. headgate understands the common ones too.

| Header | Where it comes from | What headgate does |
| --- | --- | --- |
| `retry-after-ms` | SDKs generated by Stainless, such as OpenAI's | a millisecond `Retry-After`; preferred when both are sent |
| `x-should-retry` | the same SDKs | the server's own word on whether a re-send is safe, [followed by default](retries.md) |
| `X-RateLimit-Remaining`, `X-RateLimit-Reset` | GitHub, and many others | read when `RateLimit` is absent; the reset may be seconds or a Unix time |
| `x-request-id`, `request-id`, `x-amzn-requestid`, `x-ms-request-id`, `cf-ray` | most platforms | read into every error's [`request_id`](observability.md#request-ids), for support tickets |
