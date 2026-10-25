# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
[Report a vulnerability](https://github.com/kelsayed17/headgate/security/advisories/new). Do not open a public issue.

Include what you found, how to reproduce it, and the impact you expect. You will get an acknowledgement within a
week, and a fix or a plan within 30 days for a confirmed issue.

## Supported versions

headgate is pre-1.0: only the latest release receives fixes.

## Scope

headgate sits between an application and the APIs it calls, so these are in scope:

- a credential (an auth header, a query-parameter key, a token) reaching a host other than the one it was meant for,
  being written to the HTTP cache, or appearing unredacted in a `ClientError` message;
- a response body escaping the size cap (`max_response_bytes`), including through decompression;
- a URL path built from a t-string letting an interpolated value change the path's structure;
- anything that lets one client's cached response be served to another client that should not see it.
