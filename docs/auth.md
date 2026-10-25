# Auth

A client's `auth=` is an `httpx2.Auth`. httpx2 runs its flow once per call, above the transport stack, so a retry
re-sends the request already signed. headgate ships three, each a `CredentialStamper` that declares what it stamps, so
the [credential guard](security.md#credentials-stay-on-your-origin) keeps it on your origin and the
[cache](caching.md#partitions) keeps each credential's entries apart and the credential itself out of storage.

Every one merges without overriding: a header or parameter the call sets itself wins.

## Static credentials

```python
from headgate import QueryParamAuth, StaticHeaderAuth

bearer = StaticHeaderAuth({"Authorization": f"Bearer {api_key}"})
api_key_header = StaticHeaderAuth({"x-api-key": api_key})
query_key = QueryParamAuth({"api_key": api_key})
```

`StaticHeaderAuth({})` is a no-op, so a builder with no key configured can return one rather than `None`.
`QueryParamAuth` registers its parameter names as secret, so they are redacted from every error message and kept out of
the cache; prefer a header where the provider allows one (see [security](security.md#redaction)).

## CachedTokenAuth

For a provider that mints short-lived tokens over the network. A subclass supplies the exchange; the base class gives
it:

- **one mint at a time.** Concurrent requests that need a token wait for a single mint instead of each firing one;
- **refresh before expiry**, `refresh_margin_s` (120 s) before the token expires, or at half its life when that is
  shorter, because a token expiring mid-flight fails a request that may be a write;
- **one re-mint on a 401**, for a token revoked early, and never a loop. A token minted in the last 60 seconds is not
  re-minted: a 401 on it is a permissions problem, which reaches the caller as the `AuthError` it is.

The exchange is one of two methods. `_mint_token()` makes it on a client of its own and returns `(token, lifetime)`:

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
    async def _mint_token(self) -> tuple[str, float]:
        async with httpx2.AsyncClient(timeout=10.0, trust_env=False) as http:  # read no proxy from the environment
            response = await http.post("https://login.partner.example/token", auth=self._credentials)
            response.raise_for_status()
        token = decode(response.content, Token)
        return token.access_token, token.expires_in
```

Or `_mint_flow(request)` yields the requests the exchange needs, so they travel through the owning client's own stack
(its transport, rate budget, retries and error mapping), and ends by yielding `Minted(token, lifetime)`.
[`examples/orders_client.py`](../examples/orders_client.py) has a complete one. What the flow can count on:

- **Each response arrives read**, so `.content`, `.text` and `.json()` work on it. It is read up to 1 MiB
  (`headgate.MAX_MINT_RESPONSE_BYTES`, decompressed); a larger one fails the mint with `ResponseTooLargeError`.
- **Each hop is sent with the call's timeouts** unless it sets a `timeout` extension of its own, and every hop's
  timeouts are cut to what the call's deadline leaves, so a token host that never answers cannot hold the call (or
  the other calls waiting for the token) past it.

Each hop counts toward `HttpOptions.max_redirects`, because httpx2 keeps one history per call; a mint of several hops
needs a higher limit. A hop may carry the mint's credentials to the origin it addresses and to same-origin redirects,
and nowhere else.

A failed mint is reported as the token endpoint's failure, never the API call's: a `raise_for_status()` becomes
`AuthError` (401, 403) or `UpstreamStatusError`, a network failure (in the send or reading the body) `TransportError`,
an oversized token response `ResponseTooLargeError`, and an unreadable one (a body that is not the expected shape, a
response read out of order, an ambiguous cookie) `ResponseParseError`, each naming the auth class and the token
endpoint. The API host's breaker records nothing for it.

### Other credential shapes

- `_attach(request, token)` decides how a token is put on a request (`Authorization: Bearer` by default). A subclass
  that overrides it declares the headers it sets in `token_headers`, or the class definition fails: an undeclared
  header would follow a request anywhere.
- `_skips(request)` says a request needs no token (by default, one that already carries a token header).
- `seed(token, lifetime)` caches a token obtained elsewhere, such as one restored from storage or a test's fixed bearer.
- `CachedTokenAuth[TokenT]` is generic in the token's type, `str` by default.
- `credential_identity()` names the long-lived credential for the cache partition. By default each instance is its own
  identity; return the client id and secret (hashed, never stored) to let instances holding the same credential share
  entries. Never return the short-lived token, which changes on every refresh.
- `CachedTokenAuth.reject_credential_rotation(returned, configured, provider=..., setting=...)` raises
  `ClientConfigurationError` when a token endpoint hands back a replacement for the long-lived credential, which an
  in-memory cache cannot persist.
