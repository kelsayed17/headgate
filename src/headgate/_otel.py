# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The implementation of :mod:`headgate.otel`, which exports it and documents it."""

from dataclasses import dataclass
from typing import ClassVar, Final, final
from urllib.parse import urlsplit

try:
    from opentelemetry import context as otel_context
    from opentelemetry import metrics, trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
except ImportError as exc:
    msg = "headgate.otel needs opentelemetry-api: pip install headgate[otel]"
    raise ImportError(msg) from exc

import httpx2
from opentelemetry.metrics import MeterProvider
from opentelemetry.propagators.textmap import TextMapPropagator
from opentelemetry.trace import Span, TracerProvider
from opentelemetry.util.types import AttributeValue

from headgate._core import CallContext, CallFinished, CallInfo, CallStarted, Event, check_kind, sealed
from headgate._middleware import Next
from headgate._user_agent import headgate_version
from headgate.cache._transport import TRACE_HEADERS

__all__ = ["OpenTelemetryObserver"]

#: The instrumentation scope's name, for the tracer and the meter.
_SCOPE: Final = "headgate"

#: The methods reported by name: the conventions' known methods, and QUERY, a verb of the client's own; any other is
#: reported as ``_OTHER``.
_KNOWN_METHODS: Final = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "QUERY", "TRACE"}
)

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
    parts = urlsplit(info.full_url)
    attributes[_URL_FULL] = info.full_url
    if parts.hostname is not None:
        attributes[_SERVER_ADDRESS] = parts.hostname
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    if port is not None:
        attributes[_SERVER_PORT] = port
    name = "HTTP" if method not in _KNOWN_METHODS else method
    if info.template is not None:
        attributes[_URL_TEMPLATE] = info.template
        name = f"{name} {info.template}"
    return name, attributes


def _error_type(status: int | None, error: str | None) -> str | None:
    """Return a finished call's ``error.type``: its error *status*, else its *error*'s name; ``None`` on success."""
    if error == _CANCELLED:
        return None
    if status is not None and status >= _FIRST_ERROR_STATUS:
        return str(status)
    return error


@final
@sealed("configure it with tracer_provider=, meter_provider= and propagator=, or wrap it in an observer of your own")
class OpenTelemetryObserver:
    """Report each call as an OpenTelemetry CLIENT span and duration, and propagate its trace context. See the module.

    One instance can serve any number of clients. It keeps the spans of the calls in flight, keyed by call id. As
    middleware it stamps trace context, which is no credential, so it declares none.
    """

    credential_headers: ClassVar = frozenset[str]()
    credential_params: ClassVar = frozenset[str]()

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

        :raises TypeError: for a provider or a propagator of the wrong kind.
        """
        check_kind("tracer_provider", tracer_provider, (TracerProvider, type(None)), "a TracerProvider, or None")
        check_kind("meter_provider", meter_provider, (MeterProvider, type(None)), "a MeterProvider, or None")
        check_kind("propagator", propagator, (TextMapPropagator, type(None)), "a TextMapPropagator, or None")
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
        # Named for the cache, which leaves them out of what identical GETs must agree on to share a download.
        request.extensions[TRACE_HEADERS] = self._propagator.fields
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
