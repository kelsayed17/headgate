# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""``OpenTelemetryObserver``: one CLIENT span and one duration per logical call, and trace context on every attempt.

Every test hands the observer an SDK tracer and meter provider of its own, read back through in-memory exporters, so
nothing touches OpenTelemetry's process-global providers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, SpanKind, StatusCode, format_span_id, format_trace_id

from headgate import HttpOptions, QueryParamAuth, TransportPolicy, nothing
from headgate._user_agent import headgate_version
from headgate.errors import ResponseParseError, TransportError, UpstreamStatusError
from headgate.events import AttemptFinished, CallFinished, CallInfo, CallStarted, RetryScheduled
from headgate.msgspec import decoder
from headgate.otel import OpenTelemetryObserver
from headgate.retry import NO_RETRY
from headgate.testing import FakeTransport, TestClock
from tests.observe._support import client

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.util.types import Attributes

#: How long each send takes on the test clock.
_SEND_TIME = 0.25
_HOST = "api.example.test"
_HTTPS_PORT = 443


class Telemetry:
    """An observer over an SDK tracer and meter of its own, and what they exported."""

    def __init__(self) -> None:
        """Build the providers and the observer."""
        self.spans = InMemorySpanExporter()
        self.tracer_provider = TracerProvider()
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        self.metrics = InMemoryMetricReader()
        self.meter_provider = MeterProvider(metric_readers=[self.metrics])
        self.observer = OpenTelemetryObserver(tracer_provider=self.tracer_provider, meter_provider=self.meter_provider)
        self.tracer = self.tracer_provider.get_tracer("test")

    @property
    def http(self) -> HttpOptions:
        """Return options with the observer installed."""
        return self.observer.install(HttpOptions())

    def span(self, name: str | None = None) -> ReadableSpan:
        """Return the one span exported (named *name*, when given)."""
        (span,) = [span for span in self.spans.get_finished_spans() if name is None or span.name == name]
        return span

    def duration(self) -> HistogramDataPoint:
        """Return the one ``http.client.request.duration`` data point recorded."""
        data = self.metrics.get_metrics_data()
        assert data is not None
        (metric,) = [
            metric for resource in data.resource_metrics for scope in resource.scope_metrics for metric in scope.metrics
        ]
        assert metric.name == "http.client.request.duration"
        (point,) = metric.data.data_points
        assert isinstance(point, HistogramDataPoint)
        return point


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    """Provide fresh providers, shut down afterwards."""
    found = Telemetry()
    yield found
    found.tracer_provider.shutdown()
    found.meter_provider.shutdown()


def _attributes(span: ReadableSpan) -> dict[str, object]:
    """Return *span*'s attributes as a plain dict."""
    return dict(span.attributes or {})


def _refuse(request: httpx2.Request) -> httpx2.Response:
    """Fail the connection."""
    msg = "connection refused"
    raise httpx2.ConnectError(msg, request=request)


async def test_a_call_is_one_client_span_named_by_its_route(telemetry: Telemetry) -> None:
    """Name the span ``GET <route>`` and give it the request's semantic-convention attributes."""
    series_id = "GDP/real"
    fake = FakeTransport().route("GET", "/v1/series/GDP%2Freal")
    await client(fake, http=telemetry.http).get(t"series/{series_id}", expect=nothing)

    span = telemetry.span()
    assert (span.name, span.kind, span.status.status_code) == (
        "GET series/{series_id}",
        SpanKind.CLIENT,
        StatusCode.UNSET,
    )
    assert _attributes(span) == {
        "http.request.method": "GET",
        "url.full": f"https://{_HOST}/v1/series/GDP%2Freal",
        "server.address": _HOST,
        "server.port": _HTTPS_PORT,
        "url.template": "series/{series_id}",
        "http.response.status_code": 200,
    }


async def test_the_duration_is_the_whole_call_on_the_clients_clock(telemetry: Telemetry) -> None:
    """Record one duration per call, without the high-cardinality URL attributes."""
    clock = TestClock()

    def slow(request: httpx2.Request) -> httpx2.Response:
        clock.advance(_SEND_TIME)
        return httpx2.Response(200, request=request)

    fake = FakeTransport().route("GET", "/v1/x", slow)
    with clock.install():
        built = client(fake, http=telemetry.http)
    await built.get("x", expect=nothing)

    point = telemetry.duration()
    assert (point.count, point.sum) == (1, _SEND_TIME)
    assert dict(point.attributes or {}) == {
        "http.request.method": "GET",
        "server.address": _HOST,
        "server.port": _HTTPS_PORT,
        "http.response.status_code": 200,
    }


async def test_retries_are_resends_of_one_span(telemetry: Telemetry) -> None:
    """Report every attempt in one span, counting the re-sends."""
    fake = FakeTransport().route("GET", "/v1/x", 503, 503, 200)
    await client(fake, http=telemetry.http).get("x", expect=nothing)

    span = telemetry.span()
    assert span.name == "GET"
    assert (_attributes(span)["http.request.resend_count"], _attributes(span)["http.response.status_code"]) == (2, 200)


async def test_an_error_status_is_the_error_type(telemetry: Telemetry) -> None:
    """Report a 4xx as ``error.type`` on the span, which is an error, and on the measurement."""
    fake = FakeTransport().route("GET", "/v1/x", 404)
    with pytest.raises(UpstreamStatusError):
        await client(fake, http=telemetry.http).get("x", expect=nothing)

    span = telemetry.span()
    assert (_attributes(span)["error.type"], span.status.status_code) == ("404", StatusCode.ERROR)
    assert dict(telemetry.duration().attributes or {})["error.type"] == "404"


async def test_a_status_that_outlasted_the_retries_is_reported(telemetry: Telemetry) -> None:
    """Report the last attempt's 503, which the call raised on rather than returned, as status and error type."""
    fake = FakeTransport().route("GET", "/v1/x", 503)
    with pytest.raises(UpstreamStatusError):
        await client(fake, http=telemetry.http).get("x", expect=nothing)

    attributes = _attributes(telemetry.span())
    assert (attributes["http.response.status_code"], attributes["error.type"]) == (503, "503")
    assert attributes["http.request.resend_count"] == len(fake.calls) - 1


async def test_a_transport_failure_is_named_by_its_error_class(telemetry: Telemetry) -> None:
    """Report a failure without a status as the ``ClientError`` class the call raised."""
    fake = FakeTransport().route("GET", "/v1/x", _refuse)
    with pytest.raises(TransportError):
        await client(fake, policy=TransportPolicy(retry=NO_RETRY), http=telemetry.http).get("x", expect=nothing)

    attributes = _attributes(telemetry.span())
    assert attributes["error.type"] == "TransportError"
    assert "http.response.status_code" not in attributes


async def test_an_unreadable_body_is_an_error_with_its_status(telemetry: Telemetry) -> None:
    """Report a 2xx whose body could not be read with its status and the parse error."""
    fake = FakeTransport().route("GET", "/v1/x", httpx2.Response(200, content=b"not json"))
    with pytest.raises(ResponseParseError):
        await client(fake, http=telemetry.http).get("x", expect=decoder(dict[str, int]))

    span = telemetry.span()
    assert (_attributes(span)["http.response.status_code"], _attributes(span)["error.type"]) == (
        200,
        "ResponseParseError",
    )
    assert span.status.status_code is StatusCode.ERROR


async def test_no_credential_reaches_a_span(telemetry: Telemetry) -> None:
    """Redact secret query values from ``url.full``; the auth's own parameter never reaches the span."""
    fake = FakeTransport().route("GET", "/v1/x")
    built = client(fake, http=telemetry.http, auth=QueryParamAuth({"api_key": "s3cret"}))
    await built.get("x", expect=nothing, params={"token": "t0k", "page": "2"})

    span = telemetry.span()
    assert _attributes(span)["url.full"] == f"https://{_HOST}/v1/x?token=%2A%2A%2A&page=2"
    exported = span.to_json()
    assert "s3cret" not in exported
    assert "t0k" not in exported


def _context(span: ReadableSpan) -> SpanContext:
    """Return *span*'s context, which an exported span always has."""
    context = span.get_span_context()
    assert context is not None
    return context


def _traceparent(span: ReadableSpan) -> str:
    """Return the ``traceparent`` W3C Trace Context writes for *span*."""
    context = _context(span)
    return f"00-{format_trace_id(context.trace_id)}-{format_span_id(context.span_id)}-{context.trace_flags:02x}"


async def test_every_attempt_carries_the_calls_trace_context(telemetry: Telemetry) -> None:
    """Write the call span's ``traceparent`` on each attempt, re-sends included."""
    fake = FakeTransport().route("GET", "/v1/x", 503, 200)
    await client(fake, http=telemetry.http).get("x", expect=nothing)

    expected = _traceparent(telemetry.span())
    assert [request.headers["traceparent"] for request in fake.calls] == [expected, expected]


async def test_the_call_span_is_a_child_of_the_callers_and_parent_of_the_transports(telemetry: Telemetry) -> None:
    """Start the span under the caller's current span, and make it current while an attempt is in flight."""

    def traced(request: httpx2.Request) -> httpx2.Response:
        with telemetry.tracer.start_as_current_span("network"):
            return httpx2.Response(200, request=request)

    fake = FakeTransport().route("GET", "/v1/x", traced)
    built = client(fake, http=telemetry.http)
    with telemetry.tracer.start_as_current_span("caller"):
        await built.get("x", expect=nothing)

    caller, call, network = telemetry.span("caller"), telemetry.span("GET"), telemetry.span("network")
    assert call.parent is not None
    assert network.parent is not None
    assert call.parent.span_id == _context(caller).span_id
    assert network.parent.span_id == _context(call).span_id


async def test_as_middleware_alone_it_propagates_the_callers_context(telemetry: Telemetry) -> None:
    """Write the caller's current span on a request of a call it did not observe, and nothing outside a span."""
    fake = FakeTransport().route("GET", "/v1/x")
    built = client(fake, http=HttpOptions(middleware=(telemetry.observer,)))
    await built.get("x", expect=nothing)
    with telemetry.tracer.start_as_current_span("caller"):
        await built.get("x", expect=nothing)

    outside, inside = fake.calls
    assert "traceparent" not in outside.headers
    assert inside.headers["traceparent"] == _traceparent(telemetry.span("caller"))


def test_install_adds_it_as_observer_and_outermost_middleware(telemetry: Telemetry) -> None:
    """Append it to the observers and put it first among the middleware."""

    async def other(request: httpx2.Request, call_next: object) -> httpx2.Response:
        del call_next
        return httpx2.Response(200, request=request)

    http = telemetry.observer.install(HttpOptions(middleware=(other,)))
    assert http.observers == (telemetry.observer,)
    assert http.middleware == (telemetry.observer, other)


def _finish(observer: OpenTelemetryObserver, info: CallInfo, **outcome: object) -> None:
    """Deliver *info*'s start and its finish with *outcome*'s status and error."""
    observer.notify(CallStarted(call=info))
    observer.notify(AttemptFinished(call=info, attempt=1, elapsed=0.0))
    status = outcome.get("status_code")
    error = outcome.get("error")
    observer.notify(
        CallFinished(
            call=info,
            status_code=status if isinstance(status, int) else None,
            error=error if isinstance(error, str) else None,
            attempts=1,
            elapsed=0.0,
        )
    )


def test_a_non_standard_method_is_other(telemetry: Telemetry) -> None:
    """Report an unknown method as ``_OTHER`` with the original beside it, and name the span ``HTTP``."""
    _finish(telemetry.observer, CallInfo(method="purge", url="x", full_url="http://h.test:8080/x"), status_code=200)

    span = telemetry.span()
    assert span.name == "HTTP"
    attributes: Attributes = span.attributes
    assert dict(attributes or {}) == {
        "http.request.method": "_OTHER",
        "http.request.method_original": "purge",
        "url.full": "http://h.test:8080/x",
        "server.address": "h.test",
        "server.port": 8080,
        "http.response.status_code": 200,
    }


@pytest.mark.parametrize(
    ("full_url", "expected"),
    [
        pytest.param(None, {}, id="unknown"),
        pytest.param("relative/x", {"url.full": "relative/x"}, id="no-host-no-scheme"),
    ],
)
def test_a_url_without_a_server_reports_none(
    telemetry: Telemetry, full_url: str | None, expected: dict[str, str]
) -> None:
    """Leave out the server attributes a call's URL does not give."""
    _finish(telemetry.observer, CallInfo(method="GET", url="x", full_url=full_url))
    assert _attributes(telemetry.span()) == {"http.request.method": "GET", **expected}


def test_a_cancelled_call_is_not_an_error(telemetry: Telemetry) -> None:
    """Leave a cancelled call's span status unset, with no ``error.type``."""
    _finish(telemetry.observer, CallInfo(method="GET", url="x"), error="CancelledError")

    span = telemetry.span()
    assert span.status.status_code is StatusCode.UNSET
    assert "error.type" not in _attributes(span)


def test_a_finish_it_never_saw_start_is_ignored(telemetry: Telemetry) -> None:
    """Export nothing for a ``CallFinished`` whose ``CallStarted`` it did not receive."""
    telemetry.observer.notify(CallFinished(call=CallInfo(method="GET", url="x"), attempts=1, elapsed=0.0))
    assert telemetry.spans.get_finished_spans() == ()


def test_events_it_does_not_use_are_ignored(telemetry: Telemetry) -> None:
    """Export nothing for an event that neither opens nor closes a span, or an attempt of an unknown call."""
    info = CallInfo(method="GET", url="x")
    telemetry.observer.notify(RetryScheduled(call=info, attempt=1, delay=0.0, reason="503"))
    telemetry.observer.notify(AttemptFinished(call=info, attempt=1, status_code=503, elapsed=0.0))
    assert telemetry.spans.get_finished_spans() == ()


def test_the_global_providers_are_the_default() -> None:
    """Build on the global providers and the W3C propagator when given none."""
    observer = OpenTelemetryObserver()
    _finish(observer, CallInfo(method="GET", url="x"))


async def test_a_400_is_the_first_error_status(telemetry: Telemetry) -> None:
    """Report a 400 as ``error.type``: the conventions count every status from 400 as a client span's error."""
    fake = FakeTransport().route("GET", "/v1/x", 400)
    with pytest.raises(UpstreamStatusError):
        await client(fake, http=telemetry.http).get("x", expect=nothing)

    assert _attributes(telemetry.span())["error.type"] == "400"


#: The bucket boundaries the HTTP semantic conventions advise for ``http.client.request.duration``, in seconds.
_ADVISED_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0)


async def test_spans_and_durations_are_reported_under_headgates_scope_and_the_conventions_names(
    telemetry: Telemetry,
) -> None:
    """Name the instrumentation scope ``headgate`` at its version, and the histogram as the conventions do."""
    await client(FakeTransport().route("GET", "/v1/x"), http=telemetry.http).get("x", expect=nothing)

    scope = telemetry.span().instrumentation_scope
    assert scope is not None
    assert (scope.name, scope.version) == ("headgate", headgate_version())
    data = telemetry.metrics.get_metrics_data()
    assert data is not None
    (scoped,) = [scoped for resource in data.resource_metrics for scoped in resource.scope_metrics]
    assert (scoped.scope.name, scoped.scope.version) == ("headgate", headgate_version())
    (metric,) = scoped.metrics
    assert (metric.name, metric.unit, metric.description) == (
        "http.client.request.duration",
        "s",
        "Duration of HTTP client requests.",
    )
    assert tuple(telemetry.duration().explicit_bounds) == _ADVISED_BUCKETS


async def test_a_request_of_no_call_passes_through_with_no_trace_context(telemetry: Telemetry) -> None:
    """Send a request that belongs to no call as it is, outside any span: nothing to propagate."""
    sent: list[httpx2.Request] = []

    async def call_next(request: httpx2.Request) -> httpx2.Response:
        """Record *request* and answer it."""
        sent.append(request)
        return httpx2.Response(200, request=request)

    response = await telemetry.observer(httpx2.Request("GET", "https://api.example.test/x"), call_next)

    assert response.status_code == httpx2.codes.OK
    assert "traceparent" not in sent[0].headers


async def test_the_callers_span_is_current_again_after_the_call(telemetry: Telemetry) -> None:
    """Restore the caller's current span once the call returns: the call's span is current only while it sends."""
    built = client(FakeTransport().route("GET", "/v1/x"), http=telemetry.http)
    with telemetry.tracer.start_as_current_span("caller") as caller:
        await built.get("x", expect=nothing)

        assert trace.get_current_span() is caller
