# Security

An API client holds credentials and follows URLs it did not write: redirects, cursor URLs, links in a response body.
headgate makes the safe behaviour the default and loosening it explicit.

## Credentials stay on your origin

Every auth headgate ships declares the headers and query parameters it stamps (the `CredentialStamper` protocol:
`credential_headers`, `credential_params`, `credential_identity()`). A request hook runs before **every hop**: the
first request, each redirect, and each request an auth flow sends. On any hop addressed to an origin that is neither
`base_url`'s nor in `HttpOptions(trusted_origins=...)`, it removes:

- every header and query parameter the auth declares;
- `Authorization` and `Proxy-Authorization`, whatever set them, so a third-party auth or a caller's own header does not
  travel either;
- `Cookie`, unless the client keeps a cookie jar (`cookies=True`), which scopes cookies to their domains itself.

The request still goes out, unsigned, so a signed download URL on a CDN works. A credential meant for another origin
needs that origin in `trusted_origins`, written as a bare origin:

```python
from headgate import HttpOptions

http = HttpOptions(trusted_origins=frozenset({"https://uploads.example.com", "https://eu.api.example.com:8443"}))
```

The hops a [`CachedTokenAuth`](auth.md#cachedtokenauth) mint flow yields may carry the mint's credentials to the token
endpoint its code addressed, and to the redirects that endpoint answers with on the same origin, and nowhere else.

A third-party `httpx2.Auth` that is not a `CredentialStamper` and puts its credential in another header or in the query
is not covered; implement the protocol to declare it. A cache refuses such an auth unless told the data is public (see
[caching](caching.md#partitions)).

## Redirects

- **No downgrade.** A redirect from `https` to `http` is never followed: the 3xx becomes the call's final response and
  raises as the `UpstreamStatusError` it is, with a note naming the refused target.
- **A hop limit.** `HttpOptions(max_redirects=5)` is the default. An auth flow's hops count toward it too, because
  httpx2 keeps one history per call; a client whose mint takes several hops raises it. Past it the call raises
  `TransportError` ("Exceeded maximum allowed redirects").
- **No body sent twice.** A 307 or 308 that would re-send a one-shot streamed body is not followed; it raises as its
  3xx, with a note suggesting a body factory. A 303, and a 301 or 302 answering a POST, become a body-less GET as
  usual.
- **No unbounded redirect bodies.** The body of a redirect being followed is dropped unread, so a large 302 cannot
  bypass the response size cap.
- **Off when you need it.** `HttpOptions(follow_redirects=False)`, or `follow_redirects=False` on a call, makes every
  redirect the final response: declare it with `Expect(...).on(302, ...)` or it raises.

## The SSRF guard

A client that fetches URLs it did not write (webhooks, user-supplied links, cursors from a partner) sets
`HttpOptions(allow_private_hosts=False)`. Every hop's host is resolved and refused, as `ForbiddenDestinationError`,
when any address it resolves to is not globally routable: loopback, private, link-local, carrier-grade NAT, multicast,
reserved or unspecified, with IPv4-mapped and NAT64 addresses judged by the IPv4 address they reach. Nothing is sent to
a refused host, and the refusal is never retried or charged to a breaker.

The default transport **pins** each connection to the address it vetted, in the same step as connecting, so a DNS
answer that changes between the check and the connect (DNS rebinding) cannot redirect it. The request keeps its host
name, so TLS still sends it as SNI and verifies the certificate against it, and pooled connections are keyed by name.

- A custom transport (`HttpOptions(transport=...)`, the curl transport included) resolves names itself: every hop is
  still vetted before it is sent, but the transport's own lookup is not pinned.
- The guard cannot be combined with `proxy=`, since a proxy resolves the target itself.
- `HttpOptions(resolver=...)` replaces the event loop's `getaddrinfo` (an async function of host and port returning
  addresses as text), and is accepted only with the guard on.

## Paths built from values

A t-string path escapes every interpolated value as exactly one segment:

```python
await client.delete(t"positions/{symbol}", expect=nothing)  # "BTC/USD" -> positions/BTC%2FUSD
```

So a value cannot change the URL's structure: `/`, `?`, `#` and whitespace are percent-encoded, a value of `.` or `..`
is sent as `%2E` or `%2E%2E` rather than resolved as a dot segment, and an empty value raises `ValueError` rather than
addressing the collection. A nested t-string splices as structure (`t"{API_PREFIX}/accounts"`, its own values still
escaped), and `{fragment:raw}` is the one explicit escape hatch for a pre-built fragment. A plain `str` URL is sent as
it is, which is what lets a paginator follow an absolute URL from a response.

## Redaction

Nothing headgate raises or reports carries a credential in clear:

- every URL in an error message or event has its userinfo and secret query values replaced with `***`. Secret names
  are the well-known ones (`api_key`, `token`, `access_token`, `signature`, `X-Amz-Signature`, `session`, and more,
  compared ignoring case, `_` and `-`) plus every name a `QueryParamAuth` was given;
- a header value the HTTP library refuses to encode is reported by the header's name, never its value;
- events carry no headers and no bodies; error messages carry a bounded snippet of an error response's body, which is
  the upstream's own text.

`QueryParamAuth` puts the key in the URL, and URLs are not secret to every tool: a tracing integration that records full
request URLs records it. Exclude such hosts from URL capture, or prefer a header.

## Hermetic by default

Nothing is read from the environment: no proxy variables, no `.netrc`, no `SSL_CERT_FILE`, because the HTTP client and
the default transport are built with `trust_env=False`. Cookies a response sets are neither stored nor sent back unless
`HttpOptions(cookies=True)`. TLS verification is always on; pass `HttpOptions(ssl_context=...)` for a private CA,
mutual TLS or pinning, and `proxy=` (`http`, `https` or `socks5` URL) for a proxy. Those three configure the default
transport, so combining them with `transport=` is a `ValueError`.
