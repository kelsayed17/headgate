# Copyright (c) 2026 Khaled Elsayed
# SPDX-License-Identifier: MIT

"""The option objects: what each validates, and what each makes the client do on the wire.

Construction arguments headgate hands to httpx2 (``trust_env``, the TLS context, the pool, the proxy, the redirect
limit) are observed by recording what the default transport and the ``httpx2.AsyncClient`` are built with; everything
else is observed on the requests a scripted network receives.
"""

from __future__ import annotations

import importlib.metadata
import math
import re
import ssl
import uuid
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast, override

import httpx2
import pytest

from headgate import (
    HttpOptions,
    IdempotencyKeys,
    Next,
    Rate,
    RetryPolicy,
    Route,
    SecurityOptions,
    Timeouts,
    TransportError,
    TransportPolicy,
    UpstreamStatusError,
    nothing,
)
from headgate._options import DEFAULT_LIMITS
from headgate._user_agent import DEFAULT_USER_AGENT, headgate_version
from headgate.testing import budget_for
from tests.support.clients import StubClient
from tests.support.wire import Script

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


_RATE = Rate.per_second(1000)
_BASE = "https://api.example.test"

type Rows = list[int]
_KEY = "key-1"


def _client(script: Script, *, policy: TransportPolicy | None = None, http: HttpOptions | None = None) -> StubClient:
    """Build a client over *script* with *policy* and *http*, whose transport is replaced by the script's."""
    options = replace(http or HttpOptions(), transport=script.transport())
    return StubClient(base_url=_BASE, rate=_RATE, policy=policy or TransportPolicy(), http=options)


# --- Upstream httpx objects are refused by name ---


class _UpstreamTransport(httpx2.AsyncBaseTransport):
    """Stands in for an upstream ``httpx`` transport: a class whose module is ``httpx``'s."""


_UpstreamTransport.__module__ = "httpx._transports.default"


class _UpstreamAuth(httpx2.Auth):
    """Stands in for an upstream ``httpx`` auth."""


_UpstreamAuth.__module__ = "httpx._auth"


class _UpstreamObserver:
    """Stands in for an observer an upstream ``httpx`` integration supplies."""

    def notify(self, event: object) -> None:
        """Ignore *event*."""


_UpstreamObserver.__module__ = "httpx"


class _UpstreamHook:
    """Stands in for an upstream ``httpx`` event hook passed as middleware, declaring what a middleware must."""

    credential_headers: ClassVar = frozenset[str]()
    credential_params: ClassVar = frozenset[str]()

    async def __call__(self, request: httpx2.Request, call_next: Callable[[httpx2.Request], object]) -> httpx2.Response:
        """Never run: the options refuse it first."""
        del request, call_next
        raise AssertionError


_UpstreamHook.__module__ = "httpx._client"


@pytest.mark.parametrize(
    ("build", "what"),
    [
        pytest.param(lambda: HttpOptions(transport=_UpstreamTransport()), "transport", id="transport"),
        pytest.param(
            lambda: StubClient(base_url=_BASE, rate=_RATE, observers=(_UpstreamObserver(),)), "observers", id="observer"
        ),
        pytest.param(
            lambda: StubClient(base_url=_BASE, rate=_RATE, middleware=(_UpstreamHook(),)), "middleware", id="event-hook"
        ),
        pytest.param(lambda: StubClient(base_url=_BASE, rate=_RATE, auth=_UpstreamAuth()), "auth", id="auth"),
    ],
)
def test_an_upstream_httpx_object_is_refused_by_name(build: Callable[[], object], what: str) -> None:
    """Refuse an object from upstream ``httpx`` where it is passed, saying so, rather than failing later."""
    with pytest.raises(TypeError, match=f"{what}.*upstream httpx"):
        build()


# --- HttpOptions validation ---


def _ok(request: httpx2.Request) -> httpx2.Response:
    """Answer 200."""
    return httpx2.Response(200, request=request)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: HttpOptions(proxy="ftp://proxy.example.test"),
            "proxy must be an http, https or socks5 URL (got a ftp URL)",
        ),
        (lambda: HttpOptions(proxy="http://[::1"), "proxy must be an http, https or socks5 URL (got a malformed URL)"),
        (
            lambda: HttpOptions(proxy="//p.test:8080"),
            "proxy must be an http, https or socks5 URL (got a malformed URL)",
        ),
        (lambda: Route("/"), "fragment must name at least one path segment (got '/')"),
        (lambda: Route("/x", deadline=0.0), "deadline must be > 0 and finite, or None (got 0.0)"),
        (lambda: IdempotencyKeys(header=""), "header must name a header"),
    ],
    ids=["other-scheme", "unparsable", "no-scheme", "empty-route", "route-deadline", "empty-header"],
)
def test_a_bad_option_says_exactly_what_is_wrong(build: Callable[[], object], message: str) -> None:
    """Refuse a bad option with a message naming the option and what it got."""
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        build()


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda: Timeouts(read=0.0), "read"),
        (lambda: Timeouts(write=float("nan")), "write"),
        (lambda: Timeouts(connect=0.0), "connect"),
        (lambda: Timeouts(pool=-1.0), "pool"),
        (lambda: HttpOptions(max_redirects=-1), "max_redirects"),
        (lambda: HttpOptions(user_agent=""), "user_agent"),
        (lambda: HttpOptions(user_agent="app/1\r\nX-Injected: 1"), "user_agent"),
        (lambda: HttpOptions(request_id_headers=("x-request-id", "")), "request_id_headers"),
        (lambda: HttpOptions(proxy="ftp://proxy.example.test"), "proxy"),
        (lambda: HttpOptions(proxy="http://[::1"), "proxy"),
        (lambda: SecurityOptions(trusted_origins=frozenset({"https://cdn.example.test/files"})), "trusted origin"),
        (lambda: SecurityOptions(trusted_origins=frozenset({"https://cdn.example.test?x=1"})), "trusted origin"),
        (lambda: SecurityOptions(trusted_origins=frozenset({"ftp://cdn.example.test"})), "trusted origin"),
        (lambda: SecurityOptions(trusted_origins=frozenset({"https://user@cdn.example.test"})), "trusted origin"),
        (lambda: SecurityOptions(trusted_origins=frozenset({"https://[::1"})), "trusted origin"),
        (lambda: SecurityOptions(trusted_origins=frozenset({"/relative"})), "trusted origin"),
        (lambda: TransportPolicy(deadline=0.0), "deadline"),
        (lambda: TransportPolicy(deadline=float("inf")), "deadline"),
        (lambda: Route(""), "fragment"),
        (lambda: Route("/x", deadline=0.0), "deadline"),
        (lambda: IdempotencyKeys(header=""), "header"),
    ],
)
def test_an_option_refuses_a_nonsense_value(build: Callable[[], object], match: str) -> None:
    """Fail where the option is written, not at the first request."""
    with pytest.raises(ValueError, match=match):
        build()


@pytest.mark.parametrize(
    ("build", "named"),
    [
        (lambda: HttpOptions(transport=httpx2.MockTransport(_ok), ssl_context=ssl.create_default_context()), "ssl"),
        (lambda: HttpOptions(transport=httpx2.MockTransport(_ok), limits=httpx2.Limits(max_connections=1)), "lim"),
        (lambda: HttpOptions(transport=httpx2.MockTransport(_ok), proxy="http://proxy.example.test"), "proxy"),
    ],
)
def test_default_transport_settings_cannot_ride_a_custom_transport(build: Callable[[], object], named: str) -> None:
    """Refuse TLS, pool and proxy settings next to a custom transport, which owns its own."""
    with pytest.raises(ValueError, match=named):
        build()


def test_options_freeze_and_normalize_what_they_are_given() -> None:
    """Copy headers into a read-only mapping, normalize origins and request-id names, and make sequences tuples."""
    headers = {"X-A": "1"}
    routes = [Route("/r", rate=_RATE), Route("/reports")]
    origins = frozenset({"HTTPS://CDN.example.test:443/", "http://files.example.test:8080"})
    options = HttpOptions(
        headers=headers, request_id_headers=("X-Trace-Id",), security=SecurityOptions(trusted_origins=origins)
    )
    policy = TransportPolicy(routes=tuple(routes))
    headers["X-A"] = "changed"

    assert isinstance(options.headers, MappingProxyType)
    assert options.headers == {"X-A": "1"}
    assert options.security.trusted_origins == {"https://cdn.example.test", "http://files.example.test:8080"}
    assert options.request_id_headers == ("x-trace-id",)
    assert policy.routes == tuple(routes)


# --- What reaches httpx2 ---


class _RecordingTransport(httpx2.AsyncHTTPTransport):
    """``httpx2.AsyncHTTPTransport`` recording the arguments the default transport is built with."""

    built: ClassVar[list[dict[str, object]]] = []

    def __init__(
        self, *, verify: ssl.SSLContext | bool, http2: bool, limits: httpx2.Limits, proxy: str | None, trust_env: bool
    ) -> None:
        """Record the arguments, then build the real transport with them."""
        self.built.append({"verify": verify, "http2": http2, "limits": limits, "proxy": proxy, "trust_env": trust_env})
        super().__init__(verify=verify, http2=http2, limits=limits, proxy=proxy, trust_env=trust_env)


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record what the next clients' default transports are built with."""
    monkeypatch.setattr(_RecordingTransport, "built", [])
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", _RecordingTransport)
    return _RecordingTransport.built


async def test_the_default_transport_reads_no_environment_and_takes_the_tls_pool_and_proxy(
    built: list[dict[str, object]],
) -> None:
    """Build the default transport with ``trust_env=False`` and the configured TLS context, pool and proxy."""
    context = ssl.create_default_context()
    limits = httpx2.Limits(max_connections=50, max_keepalive_connections=10)
    proxy = "http://proxy.example.test:3128"
    client = StubClient(base_url=_BASE, rate=_RATE, http=HttpOptions(ssl_context=context, limits=limits, proxy=proxy))
    await client.aclose()

    assert built == [{"verify": context, "http2": True, "limits": limits, "proxy": proxy, "trust_env": False}]


async def test_the_default_transport_verifies_tls_by_default(built: list[dict[str, object]]) -> None:
    """Verify certificates with the default context, and use the default pool, when nothing else is configured."""
    client = StubClient(base_url=_BASE, rate=_RATE)
    await client.aclose()

    assert built == [{"verify": True, "http2": True, "limits": DEFAULT_LIMITS, "proxy": None, "trust_env": False}]


# --- Cookies ---


def _setting_a_cookie() -> Script:
    """Return a network whose first answer sets a cookie."""
    return Script(httpx2.Response(200, headers={"Set-Cookie": "session=abc; Path=/"}), 200)


async def test_no_cookie_a_response_sets_is_sent_back_by_default() -> None:
    """Keep no cookie jar by default, so a cookie set by one response never rides the next request."""
    script = _setting_a_cookie()
    client = _client(script)
    await client.get("a", expect=nothing)
    await client.get("b", expect=nothing)

    assert "cookie" not in script.sent[1].headers


async def test_cookies_true_keeps_httpx2s_jar() -> None:
    """Send a set cookie back with ``cookies=True``."""
    script = _setting_a_cookie()
    client = _client(script, http=HttpOptions(cookies=True))
    await client.get("a", expect=nothing)
    await client.get("b", expect=nothing)

    assert script.sent[1].headers["cookie"] == "session=abc"


async def test_a_caller_set_cookie_header_is_still_sent() -> None:
    """Disable only the jar: a ``Cookie`` header the caller sets is sent as it is."""
    script = Script()
    await _client(script).get("a", expect=nothing, headers={"Cookie": "mine=1"})

    assert script.sent[0].headers["cookie"] == "mine=1"


# --- Redirect limit ---


async def test_max_redirects_bounds_a_redirect_chain() -> None:
    """Raise once a redirect chain exceeds ``max_redirects``."""
    script = Script(httpx2.Response(302, headers={"Location": f"{_BASE}/next"}))
    client = _client(script, policy=TransportPolicy(retry=None, breaker=None), http=HttpOptions(max_redirects=1))

    with pytest.raises(TransportError, match="redirect"):
        await client.get("start", expect=nothing)
    assert len(script.sent) == 1 + 1


# --- User-Agent ---


@pytest.mark.parametrize(
    ("product", "expected"),
    [(None, DEFAULT_USER_AGENT), ("example-app/2.1", f"example-app/2.1 {DEFAULT_USER_AGENT}")],
)
async def test_the_user_agent_names_the_product_then_headgate(product: str | None, expected: str) -> None:
    """Send headgate's product tokens, after the caller's own when one is configured."""
    script = Script()
    await _client(script, http=HttpOptions(user_agent=product)).get("a", expect=nothing)

    assert script.sent[0].headers["user-agent"] == expected
    assert DEFAULT_USER_AGENT.startswith("headgate/")
    assert " httpx2/" in DEFAULT_USER_AGENT
    assert " python/" in DEFAULT_USER_AGENT


async def test_a_user_agent_header_the_caller_sets_wins() -> None:
    """Let a ``User-Agent`` in the client's headers, or a call's, replace headgate's."""
    script = Script()
    client = _client(script, http=HttpOptions(headers={"user-agent": "client-wide/1"}))
    await client.get("a", expect=nothing)
    await client.get("b", expect=nothing, headers={"User-Agent": "per-call/1"})

    assert [request.headers.get_list("user-agent") for request in script.sent] == [["client-wide/1"], ["per-call/1"]]


def test_an_uninstalled_copy_reports_an_unknown_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fall back to ``unknown`` when headgate is not installed as a distribution (a vendored copy)."""

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert headgate_version() == "unknown"


# --- Idempotency keys ---


async def test_idempotency_keys_are_stamped_once_per_call_on_post_and_patch() -> None:
    """Stamp one key per POST or PATCH call, the same on every attempt, and none on a GET."""
    keys = iter(["k1", "k2"])
    script = Script(503, 200)
    client = _client(script, policy=TransportPolicy(idempotency=IdempotencyKeys(factory=lambda: next(keys))))
    await client.post("orders", expect=nothing)
    await client.get("orders", expect=nothing)
    await client.patch("orders/1", expect=nothing)

    assert [request.headers.get("idempotency-key") for request in script.sent] == ["k1", "k1", None, "k2"]


async def test_a_calls_own_idempotency_key_is_used_under_the_configured_header() -> None:
    """Send a call's ``idempotency_key=`` under the policy's header, or the standard one without a policy."""
    script = Script()
    await _client(script, policy=TransportPolicy(idempotency=IdempotencyKeys(header="X-Request-Key"))).put(
        "orders/1", expect=nothing, idempotency_key=_KEY
    )
    await _client(script).delete("orders/1", expect=nothing, idempotency_key=_KEY)

    assert script.sent[0].headers["x-request-key"] == _KEY
    assert script.sent[1].headers["idempotency-key"] == _KEY


async def test_an_idempotency_key_the_caller_set_in_headers_is_kept() -> None:
    """Keep a key the caller already put in ``headers=``, rather than replacing it with a generated one."""
    script = Script()
    client = _client(script, policy=TransportPolicy(idempotency=IdempotencyKeys(factory=lambda: "generated")))
    await client.post("orders", expect=nothing, headers={"Idempotency-Key": _KEY})

    assert script.sent[0].headers["idempotency-key"] == _KEY


async def test_the_default_idempotency_keys_are_random_uuids() -> None:
    """Generate a different UUID for each call by default."""
    script = Script()
    client = _client(script, policy=TransportPolicy(idempotency=IdempotencyKeys()))
    await client.post("orders", expect=nothing)
    await client.post("orders", expect=nothing)

    keys = [uuid.UUID(request.headers["idempotency-key"]) for request in script.sent]
    assert keys[0] != keys[1]


# --- Routes ---


async def _call_with_deadline(deadline: float) -> None:
    """Send a GET with *deadline* as its own ``deadline=``."""
    await _client(Script(200)).get("x", expect=nothing, deadline=deadline)


async def _route_with_deadline(deadline: float) -> None:
    """Build a ``Route`` with *deadline*."""
    Route("/x", deadline=deadline)


async def _policy_with_deadline(deadline: float) -> None:
    """Build a ``TransportPolicy`` with *deadline*."""
    TransportPolicy(deadline=deadline)


async def _view_with_deadline(deadline: float) -> None:
    """Make a view with *deadline*."""
    _client(Script(200)).with_options(deadline=deadline)


@pytest.mark.parametrize("deadline", [0.0, -1.0, math.nan, math.inf])
@pytest.mark.parametrize(
    "where", [_call_with_deadline, _route_with_deadline, _policy_with_deadline, _view_with_deadline]
)
async def test_every_place_a_deadline_is_set_refuses_the_same_values(
    where: Callable[[float], Coroutine[None, None, None]], deadline: float
) -> None:
    """Refuse a deadline that is not positive and finite, with one message, wherever it is set."""
    with pytest.raises(
        ValueError, match=f"^{re.escape(f'deadline must be > 0 and finite, or None (got {deadline})')}$"
    ):
        await where(deadline)


async def test_a_route_gives_matching_paths_their_own_retry_policy() -> None:
    """Send a matching path under the route's retry policy and every other path under the client's."""
    script = Script(503)
    policy = TransportPolicy(retry=RetryPolicy(attempts=3), routes=(Route("/reports", retry=None), Route("/x")))
    client = _client(script, policy=policy)
    with pytest.raises(UpstreamStatusError):
        await client.get("v1/reports/daily", expect=nothing)
    sends_on_the_route = len(script.sent)
    with pytest.raises(UpstreamStatusError):
        await client.get("v1/series", expect=nothing)

    assert sends_on_the_route == 1
    assert len(script.sent) - sends_on_the_route == len(("503", "503", "503"))


async def test_a_calls_retry_policy_wins_over_its_route() -> None:
    """Let a call's ``retry=RetryPolicy(...)`` win over the route's policy."""
    script = Script(503)
    client = _client(script, policy=TransportPolicy(routes=(Route("/reports", retry=None),)))
    with pytest.raises(UpstreamStatusError):
        await client.get("reports", expect=nothing, retry=RetryPolicy(attempts=2))

    assert len(script.sent) == 1 + 1


# --- Option shapes the security layer will act on ---


def test_the_security_options_have_safe_defaults() -> None:
    """Trust no extra origin and allow private hosts until configured."""
    options = HttpOptions()
    assert options.security == SecurityOptions(trusted_origins=frozenset(), block_private_hosts=False, resolver=None)


class _CountingNetwork(httpx2.AsyncBaseTransport):
    """A network that counts sends and answers 200."""

    def __init__(self) -> None:
        """Start at zero."""
        self.sends = 0

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sends += 1
        return httpx2.Response(200)


async def test_a_custom_transport_is_used_as_it_is() -> None:
    """Send through a custom transport, which owns its own pool and TLS."""
    network = _CountingNetwork()
    await StubClient(base_url=_BASE, rate=_RATE, http=HttpOptions(transport=network)).get("a", expect=nothing)
    assert network.sends == 1


def test_an_installed_copy_reports_its_distributions_version() -> None:
    """Report the version the installed ``headgate`` distribution declares."""
    assert headgate_version() == importlib.metadata.version("headgate")


@pytest.mark.parametrize(
    ("value", "named"),
    [
        (str, "the class str"),
        (list[int], "the type list[int]"),
        (Rows, "the type Rows"),
        (Any, "the type Any"),
        (1, "int"),
    ],
    ids=["class", "generic", "alias", "any", "value"],
)
def test_check_kind_names_what_it_got(value: object, named: str) -> None:
    """Name a class, a type expression and a plain value apart, so a refusal says which mistake it was."""
    with pytest.raises(TypeError) as caught:
        StubClient(base_url=_BASE, rate=cast("Any", value))
    assert str(caught.value) == f"rate= takes a Rate, such as Rate.per_second(10), a RateBudget or None (got {named})"


# Phase 4's moves: timeouts sit with the deadline, observers and middleware with the client.


def test_a_timeout_is_checked_against_the_deadline_of_its_policy() -> None:
    """Refuse a policy whose timeout is longer than its deadline where both are written, not in the client."""
    with pytest.raises(ValueError, match=r"^read timeout 120s is longer than the 60s deadline"):
        TransportPolicy(timeout=Timeouts(read=120.0), deadline=60.0)
    assert TransportPolicy(timeout=Timeouts(read=120.0), deadline=None).timeout == Timeouts(read=120.0)


@pytest.mark.parametrize("name", ["timeout", "observers", "middleware"])
def test_http_options_no_longer_take_what_moved(name: str) -> None:
    """Refuse ``HttpOptions(timeout=)``, ``observers=`` and ``middleware=``: they are the policy's and the client's."""
    moved: dict[str, Any] = {name: ()}
    with pytest.raises(TypeError, match=name):
        HttpOptions(**moved)


@pytest.mark.parametrize("name", ["observers", "middleware"])
@pytest.mark.parametrize(
    ("value", "got"),
    [("x", "str"), (object(), "object")],
    ids=["str", "lone"],
)
def test_the_client_takes_observers_and_middleware_as_sequences(name: str, value: object, got: str) -> None:
    """Refuse a lone observer or middleware, or a ``str``, where the client takes a sequence of them."""
    given: dict[str, Any] = {name: value}
    with pytest.raises(TypeError, match=rf"^{name}= takes a sequence, such as a tuple, of them \(got {got}\)$"):
        StubClient(base_url=_BASE, rate=_RATE, **given)


async def test_the_clients_observers_hear_its_calls() -> None:
    """Report a call to an observer given to the client, and run a middleware given to it on every attempt."""
    heard: list[str] = []
    seen: list[str] = []

    class _Observer:
        def notify(self, event: object) -> None:
            heard.append(type(event).__name__)

    class _Middleware:
        credential_headers: ClassVar = frozenset[str]()
        credential_params: ClassVar = frozenset[str]()

        async def __call__(self, request: httpx2.Request, call_next: Next) -> httpx2.Response:
            seen.append(request.method)
            return await call_next(request)

    script = Script()
    client = StubClient(
        base_url=_BASE,
        rate=_RATE,
        http=HttpOptions(transport=script.transport()),
        observers=[_Observer()],
        middleware=[_Middleware()],
    )
    await client.get("x", expect=nothing)

    assert ("CallStarted" in heard, "CallFinished" in heard, seen) == (True, True, ["GET"])


async def test_a_policy_of_no_retries_sends_once_and_a_calls_own_policy_wins() -> None:
    """Send every call once under ``TransportPolicy(retry=None)``, unless the call gives a policy of its own."""
    script = Script(503, 503, 200)
    client = _client(script, policy=TransportPolicy(retry=None))

    with pytest.raises(UpstreamStatusError):
        await client.get("x", expect=nothing)
    assert len(script.sent) == 1

    await client.get("x", expect=nothing, retry=RetryPolicy(attempts=3))
    assert len(script.sent) == len(("the first call's 503", "503", "200"))


@pytest.mark.parametrize("rate", [None, "absent"], ids=["none", "absent"])
def test_no_rate_paces_nothing_of_its_own(rate: str | None) -> None:
    """Build an unpaced budget of the client's own for ``rate=None`` and for no ``rate=`` at all, the same budget."""
    given: dict[str, Any] = {} if rate == "absent" else {"rate": None}
    client = StubClient(base_url=_BASE, **given)

    budget = budget_for(client)
    assert (budget.rate, budget.max_in_flight) == (Rate.per_second(1_000_000), None)


#: ``True`` where a value of another kind goes, typed so the call type-checks.
_TRUE: Any = True

#: Each configuration value of the wrong kind, the option the refusal names, and what it says it takes. The first
#: five are review finding M22's, which used to pass construction and fail at the first request, or be misread.
_WRONG_KINDS: Final[list[tuple[Callable[[], object], str]]] = [
    (lambda: TransportPolicy(idempotency=_TRUE), r"^idempotency= takes IdempotencyKeys\(\), or None"),
    (lambda: TransportPolicy(routes=cast("Any", ("/x",))), r"^routes= takes a tuple of Route"),
    (lambda: HttpOptions(transport=cast("Any", httpx2.HTTPTransport())), r"^transport= takes an httpx2\.AsyncBase"),
    (lambda: HttpOptions(request_id_headers=cast("Any", "x-request-id")), r"^request_id_headers= takes a tuple of"),
    (lambda: SecurityOptions(trusted_origins=cast("Any", "https://cdn.example")), r"^trusted_origins= takes a frozen"),
    (lambda: TransportPolicy(cache=cast("Any", "memory")), r"^cache= takes an HttpCache, or None"),
    (lambda: TransportPolicy(deadline=cast("Any", "60")), r"^deadline= takes seconds, or None for no deadline"),
    (lambda: TransportPolicy(deadline=_TRUE), r"^deadline= takes seconds, or None for no deadline"),
    (lambda: HttpOptions(headers=cast("Any", [("a", "b")])), r"^headers= takes a mapping of header names to values"),
    (lambda: HttpOptions(follow_redirects=cast("Any", "no")), r"^follow_redirects= takes a bool"),
    (lambda: HttpOptions(max_redirects=_TRUE), r"^max_redirects= takes a number of hops, an int"),
    (lambda: HttpOptions(max_response_bytes=cast("Any", 1.5)), r"^max_response_bytes= takes a number of bytes, an int"),
    (lambda: HttpOptions(response_check=cast("Any", "strict")), r"^response_check= takes a function of the response"),
    (lambda: HttpOptions(security=cast("Any", {})), r"^security= takes SecurityOptions\(\.\.\.\)"),
    (lambda: HttpOptions(ssl_context=_TRUE), r"^ssl_context= takes an ssl\.SSLContext, or None"),
    (lambda: HttpOptions(limits=cast("Any", 10)), r"^limits= takes httpx2\.Limits\(\.\.\.\)"),
    (lambda: HttpOptions(proxy=cast("Any", 8080)), r"^proxy= takes a proxy URL, or None"),
    (lambda: HttpOptions(user_agent=cast("Any", 1)), r"^user_agent= takes a product token, or None"),
    (lambda: HttpOptions(cookies=cast("Any", "yes")), r"^cookies= takes a bool"),
    (lambda: SecurityOptions(block_private_hosts=cast("Any", "no")), r"^block_private_hosts= takes a bool"),
    (lambda: SecurityOptions(block_private_hosts=True, resolver=cast("Any", "8.8.8.8")), r"^resolver= takes a resol"),
    (lambda: IdempotencyKeys(header=cast("Any", 1)), r"^header= takes a header name"),
    (lambda: IdempotencyKeys(factory=cast("Any", "uuid")), r"^factory= takes a function returning a fresh key"),
    (lambda: StubClient(base_url=cast("Any", None)), r"^base_url= takes the API root"),
    (lambda: Route(cast("Any", None)), r"^fragment= takes path segments, such as '/exports' \(got NoneType\)$"),
    (lambda: TransportPolicy(routes=cast("Any", Route("/x"))), r"^routes= takes a tuple of Route.*\(got Route\)$"),
    (lambda: StubClient(base_url=_BASE, policy=cast("Any", {})), r"^policy= takes TransportPolicy\(\.\.\.\)"),
    (lambda: StubClient(base_url=_BASE, http=cast("Any", {})), r"^http= takes HttpOptions\(\.\.\.\)"),
]
_WRONG_KIND_IDS: Final = [
    "idempotency",
    "routes",
    "sync-transport",
    "request-id-str",
    "origins-str",
    "cache",
    "deadline-str",
    "deadline-bool",
    "headers",
    "follow-redirects",
    "max-redirects-bool",
    "max-response-bytes-float",
    "response-check",
    "security",
    "ssl-context",
    "limits",
    "proxy",
    "user-agent",
    "cookies",
    "allow-private-hosts",
    "resolver",
    "header",
    "factory",
    "base-url",
    "route-fragment",
    "lone-route",
    "policy",
    "http",
]


@pytest.mark.parametrize(("build", "message"), _WRONG_KINDS, ids=_WRONG_KIND_IDS)
def test_every_option_is_checked_for_its_kind(build: Callable[[], object], message: str) -> None:
    """Refuse a configuration value of the wrong kind where it is written, naming the option and what it takes."""
    with pytest.raises(TypeError, match=message):
        build()


def test_a_one_shot_iterator_of_names_is_refused() -> None:
    """Refuse an iterator where names go, since reading it to check it would leave nothing to keep."""
    with pytest.raises(TypeError, match=r"^request_id_headers= takes a tuple of header names.*\(got generator\)$"):
        HttpOptions(request_id_headers=cast("Any", (name for name in ("x-trace-id",))))


def test_the_ssrf_guard_is_switched_on_by_block_private_hosts() -> None:
    """Refuse the old double negative, ``allow_private_hosts=``, and keep the guard off unless asked for."""
    old: dict[str, Any] = {"allow_private_hosts": False}
    with pytest.raises(TypeError, match="allow_private_hosts"):
        SecurityOptions(**old)
    assert SecurityOptions().block_private_hosts is False


def test_a_trusted_origin_may_be_a_url() -> None:
    """Take an ``httpx2.URL`` among the trusted origins at runtime, as main did, and normalize it as text is."""
    options = SecurityOptions(trusted_origins=cast("Any", frozenset({httpx2.URL("HTTPS://CDN.example:443/")})))
    assert options.trusted_origins == {"https://cdn.example"}
