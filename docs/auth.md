# Auth

A client's `auth=` is an `httpx2.Auth`. httpx2 runs its flow once per call, above the transport stack, so a retry
re-sends the request already signed. headgate ships five (the three static ones below, `SigningAuth` and
`CachedTokenAuth`), each a
`CredentialStamper` that declares what it stamps, so the [credential guard](security.md#credentials-stay-on-your-origin)
keeps it on your origin and the [cache](caching.md#partitions) keeps each credential's entries apart and the credential
itself out of storage.

Every one merges without overriding: a header or parameter the call sets itself wins.

## Static credentials

```python
from headgate import BearerAuth, QueryParamAuth, StaticHeaderAuth

bearer = BearerAuth(api_key)
api_key_header = StaticHeaderAuth({"x-api-key": api_key})
query_key = QueryParamAuth({"api_key": api_key})
```

`StaticHeaderAuth({})` is a no-op, so a builder with no key configured can return one rather than `None`.
`QueryParamAuth` registers its parameter names as secret, so they are redacted from every error message and kept out of
the cache; prefer a header where the provider allows one (see [security](security.md#redaction)).

## SigningAuth

For a provider that signs every request with a long-lived key: a JWT bound to the method and path, an HMAC or Ed25519
signature over a timestamp and the body. A subclass supplies the signer, `sign(request)`, and declares what it sets;
the base class gives it:

- **every attempt signed afresh**, inside the retry layer. Each attempt is signed as it enters headgate's transport
  stack, after the last [middleware](configuration.md#middleware) and before the cache and any rate-limit wait, so a
  retry or a hedge carries a signature of its own and a short-lived one (a timestamp checked to 30 s) does not expire
  across the backoff. The signature is applied before the
  [credential guard](security.md#credentials-stay-on-your-origin) runs, so a redirect hop to an origin you do not
  trust goes out without it;
- **the body, read**, so the signature can cover `request.content`;
- **a caller's signature left alone.** A request that already carries any of the signed headers or parameters is sent
  as it is, on every attempt;
- **one re-send on a 401**, signed afresh, and never a loop. A 401 to a request sent without its signature (to an
  origin the credential guard withheld it from) is not re-sent.

```python
import hashlib
import hmac
import time
from typing import ClassVar, override

import httpx2

from headgate import SigningAuth


class ExchangeAuth(SigningAuth):
    signed_headers: ClassVar = frozenset({"X-Api-Key", "X-Timestamp", "X-Signature"})

    def __init__(self, api_key: str, secret: bytes) -> None:
        self._api_key, self._secret = api_key, secret

    @override
    def sign(self, request: httpx2.Request) -> None:
        timestamp = str(int(time.time()))
        message = timestamp.encode() + request.method.encode() + request.url.raw_path + request.content
        request.headers["X-Api-Key"] = self._api_key
        request.headers["X-Timestamp"] = timestamp
        request.headers["X-Signature"] = hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    @override
    def credential_identity(self) -> bytes:
        return self._secret
```

- `sign` replaces what an earlier call set, since it runs once per attempt, and again on the re-send after a 401. An
  exception it raises surfaces from inside the retry layer as it was raised: the attempt counts (observers see an
  `AttemptFinished` with its type as the error), the request is not sent, and it is not retried. An `httpx2.HTTPError`
  or an `OSError` is the exception: the retry layer takes it for a failed send and judges it as one.
- A `SigningAuth` signs only when it is the client's own `auth=` under a headgate client. Given to a bare
  `httpx2.AsyncClient`, or wrapped inside another `httpx2.Auth`, its requests go out unsigned, and a 401 is not
  re-sent.
- `signed_headers` and `signed_params` declare what `sign` sets, so the credential guard withholds exactly those from
  an origin you do not trust, and the cache keeps them out of storage. A subclass that declares neither fails when
  it is defined. Signed parameters are registered as secret, so error messages redact them.
- `skips(request)` says a request is sent unsigned: by default, one already carrying a signed header or parameter.
  Override it for requests that need no signature, such as a provider's public reads.
- `credential_identity()` names the key for the cache partition. By default each instance is its own identity; return
  the key's secret material (hashed, never stored) to let instances holding the same key share entries.

## CachedTokenAuth

For a provider that mints short-lived tokens over the network. A subclass supplies the exchange; the base class gives
it:

- **one mint at a time.** Concurrent requests that need a token wait for a single mint instead of each firing one;
- **refresh before expiry**, `refresh_margin` (120 s) before the token expires, or at half its life when that is
  shorter, because a token expiring mid-flight fails a request that may be a write;
- **one re-mint on a 401**, for a token revoked early, and never a loop. A token minted in the last 60 seconds is not
  re-minted: a 401 on it is a permissions problem, which reaches the caller as the `AuthError` it is. Nor is a token
  re-minted for a 401 to a request sent without it, such as a call to an origin you do not trust, where the credential
  guard withheld it.

The exchange is one of two methods. `mint_token()` makes it on a client of its own and returns `(token, lifetime)`:

```python
from typing import override

import httpx2
import msgspec

from headgate import CachedTokenAuth
from headgate.msgspec import decode


class Token(msgspec.Struct, frozen=True):
    access_token: str
    expires_in: float


class PartnerAuth(CachedTokenAuth):
    def __init__(self, client_id: str, client_secret: str) -> None:
        super().__init__()
        self._credentials = (client_id, client_secret)

    @override
    async def mint_token(self) -> tuple[str, float]:
        async with httpx2.AsyncClient(timeout=10.0, trust_env=False) as http:  # read no proxy from the environment
            response = await http.post("https://login.partner.example/token", auth=self._credentials)
            response.raise_for_status()
        token = decode(response.content, Token)
        return token.access_token, token.expires_in
```

Or `mint_flow(request)` yields the requests the exchange needs, so they travel through the owning client's own stack
(its transport, rate budget, retries and error mapping), and ends by yielding `Minted(token, lifetime)`.
[`examples/orders_client.py`](https://github.com/kelsayed17/headgate/blob/main/examples/orders_client.py) has a complete
one. What the flow can count on:

- **Each response arrives read**, so `.content`, `.text` and `.json()` work on it. It is read up to 1 MiB
  (`headgate.MAX_MINT_RESPONSE_BYTES`, decompressed); a larger one fails the mint with `ResponseTooLargeError`.
- **Each hop is sent with the call's timeouts** unless it sets a `timeout` extension of its own, and every hop's
  timeouts are cut to what the call's deadline leaves, so a token host that never answers cannot hold the call (or
  the other calls waiting for the token) past it.

Each hop counts toward `HttpOptions.max_redirects`, because httpx2 keeps one history per call; a mint of several hops
needs a higher limit. A hop may carry the mint's credentials to the origin it addresses and to same-origin redirects,
and nowhere else: a redirect to another origin is refused before it is sent, as `ForbiddenDestinationError`, since a
client secret in the hop's own headers or form body would go with it. A flow that means to follow it yields the
redirected request itself.

A failed mint is reported as the token endpoint's failure, never the API call's: a `raise_for_status()` becomes
`AuthError` (401, 403) or `UpstreamStatusError`, a network failure (in the send or reading the body) `TransportError`,
an oversized token response `ResponseTooLargeError`, and an unreadable one (a body that is not the expected shape, a
response read out of order, an ambiguous cookie) `ResponseParseError`, each naming the auth class and the token
endpoint. The API host's breaker records nothing for it.

### Other credential shapes

- `attach(request, token)` decides how a token is put on a request (`Authorization: Bearer` by default). A subclass
  that overrides it declares the headers it sets in `token_headers`, or the class definition fails: an undeclared
  header would follow a request anywhere.
- `skips(request)` says a request needs no token (by default, one that already carries a token header).
- `seed(token, lifetime)` caches a token obtained elsewhere, such as one restored from storage or a test's fixed bearer.
- `CachedTokenAuth[TokenT]` is generic in the token's type, `str` by default.
- `credential_identity()` names the long-lived credential for the cache partition. By default each instance is its own
  identity; return the client id and secret (hashed, never stored) to let instances holding the same credential share
  entries. Never return the short-lived token, which changes on every refresh.
- `self.reject_credential_rotation(returned, configured, setting=...)`, called from your `mint_token`, raises
  `ClientConfigurationError` when a token endpoint hands back a replacement for the long-lived credential, which an
  in-memory cache cannot persist.
