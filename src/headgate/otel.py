# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""OpenTelemetry for headgate clients: one CLIENT span and one duration measurement per logical call.

Install it once on the client's options::

    otel = OpenTelemetryObserver()
    http = otel.install(HttpOptions(...))      # adds it to observers= and middleware=

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

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

try:
    from opentelemetry import context as otel_context
    from opentelemetry import metrics, trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
except ImportError as exc:
    msg = "headgate.otel needs opentelemetry-api: pip install headgate[otel]"
    raise ImportError(msg) from exc

from headgate._core import CallContext, CallFinished, CallInfo, CallStarted
from headgate._user_agent import headgate_version

if TYPE_CHECKING:
    import httpx2
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.propagators.textmap import TextMapPropagator
    from opentelemetry.trace import Span, TracerProvider
    from opentelemetry.util.types import AttributeValue

    from headgate._core import Event
    from headgate._middleware import Next
    from headgate._options import HttpOptions

__all__ = ["OpenTelemetryObserver"]

#: The instrumentation scope's name, for the tracer and the meter.
_SCOPE: Final = "headgate"

#: The methods the conventions name; any other is reported as ``_OTHER``.
_KNOWN_METHODS: Final = frozenset({"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"})

#: The ports a URL without one implies.
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}

#: The bucket boundaries the conventions advise for ``http.client.request.duration``, in seconds.
_DURATION_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0)

#: The first status the conventions report as an error on a client span.
_FIRST_ERROR_STATUS: Final = 400

#: What a call that was cancelled (not timed out) raises: neither a span error nor an ``error.type``.
_CANCELLED: Final = "CancelledError"

# The semantic-convention attribute names.
_METHOD: Final = "http.request.method"
_METHOD_ORIGINAL: Final = "http.request.method_original"
_SERVER_ADDRESS: Final = "server.address"
_SERVER_PORT: Final = "server.port"
_URL_FULL: Final = "url.full"
_URL_TEMPLATE: Final = "url.template"
_STATUS_CODE: Final = "http.response.status_code"
_RESEND_COUNT: Final = "http.request.resend_count"
_ERROR_TYPE: Final = "error.type"

type _Attributes = dict[str, AttributeValue]


@dataclass(frozen=True, slots=True)
class _LiveCall:
    """A call whose span is open: the span, and the attributes its measurement will carry."""

    span: Span
    metric_attributes: _Attributes


def _request_attributes(info: CallInfo) -> tuple[str, _Attributes]:
    """Return the span name and the attributes *info* gives a call's span when it starts."""
    method = info.method.upper()
    attributes: _Attributes = {}
    if method in _KNOWN_METHODS:
        attributes[_METHOD] = method
    else:
        attributes[_METHOD] = "_OTHER"
        attributes[_METHOD_ORIGINAL] = info.method
    if info.full_url is not None:
        parts = urlsplit(info.full_url)
        attributes[_URL_FULL] = info.full_url
        if parts.hostname is not None:
            attributes[_SERVER_ADDRESS] = parts.hostname
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
        if port is not None:
            attributes[_SERVER_PORT] = port
    name = "HTTP" if method not in _KNOWN_METHODS else method
    if info.route is not None:
        attributes[_URL_TEMPLATE] = info.route
        name = f"{name} {info.route}"
    return name, attributes


def _error_type(status: int | None, error: str | None) -> str | None:
    """Return a finished call's ``error.type``: its error *status*, else its *error*'s name; ``None`` on success."""
    if error == _CANCELLED:
        return None
    if status is not None and status >= _FIRST_ERROR_STATUS:
        return str(status)
    return error


class OpenTelemetryObserver:
    """Report each call as an OpenTelemetry CLIENT span and duration, and propagate its trace context. See the module.

    One instance can serve any number of clients. It keeps the spans of the calls in flight, keyed by call id.
    """

    def __init__(
        self,
        *,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        propagator: TextMapPropagator | None = None,
    ) -> None:
        """Report through *tracer_provider* and *meter_provider* (the global ones by default).

        *propagator* writes the trace context onto each attempt; W3C Trace Context by default rather than the global
        propagator, which also sends Baggage, an application's own key-value data, to every server called.
        """
        version = headgate_version()
        self._tracer = trace.get_tracer(_SCOPE, version, tracer_provider)
        meter = metrics.get_meter(_SCOPE, version, meter_provider)
        self._duration = meter.create_histogram(
            "http.client.request.duration",
            unit="s",
            description="Duration of HTTP client requests.",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self._propagator = propagator or TraceContextTextMapPropagator()
        self._live: dict[int, _LiveCall] = {}

    def install(self, http: HttpOptions) -> HttpOptions:
        """Return *http* with this observer added as an observer and as the outermost middleware."""
        return replace(http, observers=(*http.observers, self), middleware=(self, *http.middleware))

    def notify(self, event: Event) -> None:
        """Open a span on ``CallStarted``; close it and record the duration on ``CallFinished``."""
        if isinstance(event, CallStarted):
            self._start(event.call)
        elif isinstance(event, CallFinished):
            self._finish(event)

    def _start(self, info: CallInfo) -> None:
        """Open *info*'s span, a child of the caller's current span."""
        name, attributes = _request_attributes(info)
        span = self._tracer.start_span(name, kind=SpanKind.CLIENT, attributes=attributes)
        metric_attributes = {key: value for key, value in attributes.items() if key not in {_URL_FULL, _URL_TEMPLATE}}
        self._live[info.call_id] = _LiveCall(span, metric_attributes)

    def _finish(self, event: CallFinished) -> None:
        """Close the call's span with its outcome, and record its duration."""
        live = self._live.pop(event.call.call_id, None)
        if live is None:
            return
        status = event.status_code
        outcome: _Attributes = {}
        if status is not None:
            outcome[_STATUS_CODE] = status
        if (error_type := _error_type(status, event.error)) is not None:
            outcome[_ERROR_TYPE] = error_type
        span = live.span
        span.set_attributes(outcome)
        if event.attempts > 1:
            span.set_attribute(_RESEND_COUNT, event.attempts - 1)
        if error_type is not None:
            span.set_status(Status(StatusCode.ERROR))
        span.end()
        self._duration.record(event.elapsed, {**live.metric_attributes, **outcome})

    async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
        """Send one attempt of *request* with its call's span current and its trace context in the headers.

        A request of no call this observer saw (an auth flow's token mint, a client that has this as middleware but
        not as an observer) carries the caller's current context instead, if there is one.
        """
        call = CallContext.find(request)
        live = None if call is None else self._live.get(call.info.call_id)
        if live is None:
            self._propagator.inject(request.headers)
            return await call_next(request)
        current = trace.set_span_in_context(live.span)
        self._propagator.inject(request.headers, context=current)
        token = otel_context.attach(current)
        try:
            return await call_next(request)
        finally:
            otel_context.detach(token)
