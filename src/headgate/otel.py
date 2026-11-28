# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""OpenTelemetry for headgate clients: one CLIENT span and one duration measurement per logical call.

Pass it to the client twice, once as each of the two things it is::

    otel = OpenTelemetryObserver()
    client = Client(base_url=..., observers=(otel,), middleware=(otel,))

It is two things, so it goes in two places. As an :class:`~headgate.events.Observer` it turns a call's events into a
span named by the HTTP client semantic conventions (``GET``, or ``GET series/{series_id}`` when the URL was a t-string)
that starts when the call starts and ends when it ends, so it covers every attempt, every backoff wait, the auth flow
and the body read; and into a ``http.client.request.duration`` measurement. As a :class:`~headgate.Middleware` it
runs once per attempt, inside the retry layer, and writes the span's W3C Trace Context (``traceparent``, and
``tracestate`` when there is one) onto the request, so every re-send carries it. It is middleware rather than an
``httpx2`` request hook because a hook runs once per call and redirect hop, above the retry layer, and so never sees a
re-send. While an attempt is in flight the span is the current span, so spans started below it (a physical-request
instrumentation, a transport's own) are its children.

**Attributes** (copied from the stable HTTP semantic conventions; there is no ``opentelemetry-semantic-conventions``
dependency): ``http.request.method`` (``_OTHER`` plus ``http.request.method_original`` for a non-standard method),
``server.address``, ``server.port``, ``url.full`` (with credentials redacted: userinfo, secret query values),
``url.template`` (the t-string route, low-cardinality by construction), ``http.response.status_code``,
``http.request.resend_count`` (sends after the first, when there were any), and ``error.type``: the status code as
text for a 4xx or 5xx, else the name of the ``ClientError`` class the call raised (``TransportError``,
``ClientTimeoutError``, ``CircuitOpenError``, ``ResponseParseError``...). A failed call's span status is ``ERROR``; a
cancelled call's is left unset. The metric carries the same attributes less ``url.full`` and ``url.template``, as
the conventions require. Nothing in a span or measurement is a header or a body.

**One logical span, not one per attempt.** The conventions prefer a span per physical send; headgate's retries happen
inside a transport, so it reports the logical call, as they allow, with ``url.full`` the URL first requested. For
per-attempt spans as well, add ``opentelemetry-instrumentation-httpx``'s ``HTTPX2ClientInstrumentor().instrument()``:
it patches the network transport, below headgate's stack, so its spans nest under headgate's and its own
``traceparent`` (the innermost) is what reaches the server. Both record ``http.client.request.duration``, per call and
per send, so give one of them a no-op meter provider (``meter_provider=NoOpMeterProvider()`` here). Its
``instrument_client`` needs the ``httpx2.AsyncClient``, which headgate keeps private: use the global form.

**Backends.** Anything that consumes the OpenTelemetry API sees these spans: an SDK ``TracerProvider`` with any
exporter, Logfire (``logfire.configure()`` installs the global providers this uses by default) and Sentry's
OpenTelemetry integration. Sentry's and Logfire's own ``httpx`` integrations patch upstream ``httpx``, which headgate
does not use.

Requires the ``otel`` extra: ``pip install headgate[otel]`` (``opentelemetry-api`` only; bring your own SDK).
"""

from headgate._core import rehome as _rehome
from headgate._otel import OpenTelemetryObserver

__all__ = ["OpenTelemetryObserver"]

_rehome(__name__, globals())
