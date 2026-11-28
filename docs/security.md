# Security

An API client holds credentials and follows URLs it did not write: redirects, cursor URLs, links in a response body.
headgate makes the safe behavior the default and loosening it explicit.

## At a glance

| Threat | What headgate does | Your control |
| --- | --- | --- |
| :lucide-key-round: a credential sent to another host by a redirect or a link | [withholds it on every hop](#credentials-stay-on-your-origin) to an origin you did not trust | trust more origins with `trusted_origins=` |
| :lucide-arrow-down-to-line: an https page redirecting to plain http | [refuses the redirect](#redirects) | always on |
| :lucide-server-crash: a URL that reaches your internal network (SSRF) | [refuses private addresses](#the-ssrf-guard), with DNS pinning, once you turn the guard on | `block_private_hosts=True` turns it on |
| :lucide-folder-tree: a value like `../admin` rewriting the path | [escapes each value](#paths-built-from-values) as one segment | `{value:raw}` for a pre-built fragment |
| :lucide-eye-off: a token printed in a log or error | [redacts it](#redaction) as `***` in every error and event | always on |
| :lucide-package-x: a proxy or `.netrc` picked up from the environment | [ignores the environment](#hermetic-by-default) | pass `proxy=` or `ssl_context=` explicitly |
| :lucide-file-down: a huge response exhausting memory | [caps a buffered body](configuration.md#httpoptions) at 32 MiB | `max_response_bytes=` |
| :lucide-database: one user's cached answer served to another | [partitions the cache](caching.md#partitions) by credential | `Partition.SHARED` for public data |

Every hop goes through the same checks, not only the first request: each redirect, each next page and each request an
auth flow sends.

<ol class="hg-pipeline">
  <li><strong>Origin check</strong><span>not your origin or a trusted one? Credentials, <code>Authorization</code>,
    <code>Cookie</code> and client-wide headers are removed, and it goes out unsigned</span></li>
  <li><strong>Address check</strong><span>with the SSRF guard on, every address the host resolves to must be public, or
    nothing is sent</span></li>
  <li><strong>Pinned connect</strong><span>the connection goes to the address that was checked, so DNS cannot swap it</span></li>
  <li><strong>Response check</strong><span>an https-to-http redirect is refused, and a one-shot body is never sent twice</span></li>
</ol>

## Credentials stay on your origin

Every auth declares the headers and query parameters it stamps: `auth=` takes only a `headgate.Auth`, whose
`credential_headers`, `credential_params` and `credential_identity()` say what it stamps and whose credential it is, so
a plain `httpx2.Auth` is refused when the client is built. A request hook runs before **every hop**: the
first request, each redirect, and each request an auth flow sends. On any hop addressed to an origin that is neither
`base_url`'s nor in `HttpOptions(security=SecurityOptions(trusted_origins=...))`, it removes:

- every header and query parameter the auth declares;
- `Authorization` and `Proxy-Authorization`, whatever set them, so a caller's own header does not travel either;
- `Cookie`. A client that keeps a cookie jar (`cookies=True`) then sends the cookies its jar holds for that origin,
  since the jar scopes them to their domains; a `Cookie` header you set yourself, client-wide or on a call, is not the
  jar's and stays off;
- every header set in `HttpOptions(headers=...)`, where a static key is often configured, except `User-Agent` and the
  content negotiation headers `Accept`, `Accept-Encoding` and `Accept-Language`: those name the client and the format it
  wants, never a caller, so they go everywhere. Any other name is withheld, whatever it holds; a client-wide header
  another origin needs (a tracing or tenant header, say) needs that origin in `trusted_origins`;
- the same, for every header a view sets (`with_options(headers=...)`), and for every header a call sets itself
  (`headers=`) on a hop to another origin than the one the call named: a redirect, or a later page of a walk, whose URL
  the server chose. A call that names another origin's URL itself still sends it its own headers.

The request still goes out, unsigned, so a signed download URL on a CDN works. A credential meant for another origin
needs that origin in `trusted_origins`, written as a bare origin:

```python
from headgate import HttpOptions, SecurityOptions

http = HttpOptions(
    security=SecurityOptions(
        trusted_origins=frozenset({"https://uploads.example.com", "https://eu.api.example.com:8443"})
    )
)
```

The hops a [`CachedTokenAuth`](auth.md#cachedtokenauth) mint flow yields may carry the mint's credentials to the token
endpoint its code addressed, and to the redirects that endpoint answers with on the same origin, and nowhere else. A
mint hop's redirect to any other origin is refused before it is sent, as `ForbiddenDestinationError`, rather than
followed unsigned: the hop's own headers and form body (a client secret) are credentials the guard cannot tell apart,
and httpx2 sends the body again on a 307 or 308.

Every [middleware](configuration.md#middleware) declares the credentials it stamps (a key, a token) the
same way, with `credential_headers` and `credential_params`, each empty when it stamps none; `Client()` refuses one
that does not. A middleware runs inside the transport stack, after the request hook, so
the guard runs a second time below the last middleware, on every attempt of every hop, and removes what it declares
from any hop not addressed to `base_url`'s origin or a trusted one. An auth flow's token host is not trusted for a
middleware's credentials: the middleware did not choose it. A [`SigningAuth`](auth.md#signingauth) signs each attempt
below the rate budget, and the guard runs once more after it, so its signature is withheld from an untrusted hop the
same way.

## Redirects

- **No downgrade.** A redirect from `https` to `http` is never followed: the 3xx becomes the call's final response and
  raises as the `UpstreamStatusError` it is, with a note naming the refused target. A next page an `https` page names
  on `http` is refused the same way, before it is sent, as `ForbiddenDestinationError`.
- **A hop limit.** `HttpOptions(max_redirects=5)` is the default. An auth flow's hops count toward it too, because
  httpx2 keeps one history per call; a client whose mint takes several hops raises it. Past it the call raises
  `TransportError` ("Exceeded maximum allowed redirects").
- **No body sent twice.** A redirect that would re-send a one-shot streamed body (a 307 or 308, or a 301 answering
  anything but a POST) is not followed; it raises as its 3xx, with a note suggesting a body factory. A 303, a 302
  answering anything but a HEAD, and a 301 answering a POST become a body-less GET, as httpx2 follows them.
- **No unbounded redirect bodies.** The body of a redirect being followed is dropped unread, so a large 302 cannot
  bypass the response size cap.
- **Off when you need it.** `HttpOptions(follow_redirects=False)`, or `follow_redirects=False` on a call, makes every
  redirect the final response: declare it with `Expect(...).on(302, ...)` or it raises.

## The SSRF guard

A client that fetches URLs it did not write (webhooks, user-supplied links, cursors from a partner) sets
`HttpOptions(security=SecurityOptions(block_private_hosts=True))`. Every hop's host is resolved and refused, as
`ForbiddenDestinationError`, when any address it resolves to is not globally routable: loopback, private, link-local,
IPv6 site-local, carrier-grade NAT, multicast, reserved or unspecified, with IPv4-mapped and NAT64 addresses judged by
the IPv4 address they reach. Nothing is sent to a refused host, and the refusal is never retried or charged to a breaker.

The default transport **pins** each connection to the address it vetted, in the same step as connecting, so a DNS
answer that changes between the check and the connect (DNS rebinding) cannot redirect it. The request keeps its host
name, so TLS still sends it as SNI and verifies the certificate against it, and pooled connections are keyed by name.

- The curl transport (`headgate.curl.AsyncCurlTransport`) pins too: each request's host is vetted through the guard,
  and curl is handed the vetted addresses, so it never looks the name up itself. curl keeps one DNS cache and one
  connection pool for all its requests, so a curl transport serves one guard: building a second client on the same
  transport instance with a different `SecurityOptions` guard, or without the guard, raises `ValueError`. Clients with
  the same guard may share it. A `CurlOpt.PROXY`, `PRE_PROXY`, `RESOLVE` or `CONNECT_TO` in its `curl_options` chooses
  the address itself, so the guard refuses them.
- Any other custom transport (`HttpOptions(transport=...)`) resolves names itself: every hop is still vetted before
  it is sent, but the transport's own lookup is not pinned.
- The guard cannot be combined with `proxy=`, since a proxy resolves the target itself.
- `HttpOptions(security=SecurityOptions(resolver=...))` replaces the event loop's `getaddrinfo` (an async function of
  host and port returning addresses as text), and is accepted only with the guard on. Inside a test's
  `fake_network`, the fake network resolves every name in its place (see
  [testing](testing.md#names-inside-the-fake-network)), so a guarded SDK's tests make no DNS query.
- `SecurityOptions(check=...)` replaces the judgment of each address, a function of the address as text returning why
  it is refused, or `None` to admit it; one of your own is accepted only with the guard on. Every hop, over every
  transport, is judged by it. The default is `headgate.security.forbidden_reason` itself, which says why an address is
  refused (`"loopback"`, `"site-local"`, ...), so a check can decide a few addresses and defer to it for the rest, such
  as a test that reaches a loopback server:

  ```python
  from headgate import HttpOptions, SecurityOptions
  from headgate.security import forbidden_reason


  def loopback_too(address: str) -> str | None:
      return None if address == "127.0.0.1" else forbidden_reason(address)


  http = HttpOptions(security=SecurityOptions(block_private_hosts=True, check=loopback_too))
  ```

  `Resolver` and `AddressCheck`, in `headgate.security`, are the types of the two functions.

## Paths built from values

A t-string path escapes every interpolated value as exactly one segment:

```python
await client.delete(t"positions/{symbol}", expect=nothing)  # "BTC/USD" -> positions/BTC%2FUSD
```

So a value cannot change the URL's structure: `/`, `?`, `#` and whitespace are percent-encoded, a value of `.` or `..`
is sent as `%2E` or `%2E%2E` rather than resolved as a dot segment, and an empty value raises `ValueError` rather than
addressing the collection. A nested t-string splices as structure (`t"{API_PREFIX}/accounts"`, its own values still
escaped), and `{fragment:raw}` is the one explicit escape hatch for a pre-built fragment. A literal or an absolute
`httpx2.URL` is sent as it is, which is what lets a paginator follow an absolute URL from a response: it forwards that
URL as an `httpx2.URL`. A relative `httpx2.URL` raises `TypeError`, since all it could do is skip the escaping
(`httpx2.URL(f"items/{uid}")` with `uid = "../admin"` reaches `/admin`), and a URL of any other kind (`None`, `bytes`)
raises `TypeError` rather than being sent as its text.

## Redaction

Nothing headgate raises or reports carries a credential in clear:

- every URL in an error message or event has its userinfo and secret query values replaced with `***`. Secret names
  are the well-known ones (`api_key`, `token`, `access_token`, `signature`, `X-Amz-Signature`, `session`, and more,
  compared ignoring case, `_` and `-`) plus every name a `QueryParamAuth` was given;
- a header value the HTTP library refuses to encode is reported by the header's name, never its value;
- events carry no headers and no bodies; error messages carry a bounded snippet of an error response's body, which is
  the upstream's own text.

`QueryParamAuth` puts the key in the URL, and URLs are not secret to every tool: a tracing integration that records full
request URLs records it. Exclude such hosts from URL capture, or prefer a header. httpx2's own `HTTP Request:` log line
(INFO, on the `httpx2` logger) is redacted: importing headgate adds a filter to that logger that replaces the URL's
secret values with `***`, so a key or a query signature does not reach an application's INFO log. The filter is
process-wide: it applies to every httpx2 client in the process, not only headgate's.

## Hermetic by default

Nothing is read from the environment: no proxy variables, no `.netrc`, no `SSL_CERT_FILE`, because the HTTP client and
the default transport are built with `trust_env=False`. Cookies a response sets are neither stored nor sent back unless
`HttpOptions(cookies=True)`. TLS verification is always on; pass `HttpOptions(ssl_context=...)` for a private CA,
mutual TLS or pinning, and `proxy=` (`http`, `https` or `socks5` URL) for a proxy. `ssl_context=`, `proxy=` and
`limits=` configure the default transport, so combining any of them with `transport=` is a `ValueError`.
