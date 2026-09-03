"""Tracing observes the system. It never becomes part of it.

Two questions, and they are the ones that decide whether tracing is safe to
turn on in production:

**What does a request actually produce?** One server span, named for the route
template rather than the URL, carrying a method, a route and a status and
nothing else. A 404 collapses into one span name instead of inventing one per
path a scanner tried.

**What happens when the collector is not there?** Nothing. Not a slower
request, not a failed one, not a retried job, not an altered payment. That is
asserted with an exporter that *raises* — the harshest form of the failure —
because "the collector is down" is the normal state of a collector at the exact
moment an incident makes somebody want traces.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import tracing
from app.core.config import Settings
from app.core.request_metrics import UNMATCHED
from app.core.telemetry import CallOutcome, Provider, ProviderCall
from app.core.tracing import (
    HTTP_METHOD,
    HTTP_ROUTE,
    HTTP_STATUS,
    SpanKind,
    TracingConfigurationError,
    configure_tracing,
    span,
)
from app.workers.dispatch import job_span
from app.workers.queue import AgentJob, AgentQueue, JobEnvelope
from tests.fake_queue_redis import FakeQueueRedis
from tests.tracing_recorder import ExplodingExporter, Recording, install, recording_spans

pytestmark = pytest.mark.integration

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def record() -> Iterator[Recording]:
    yield from recording_spans()


@pytest_asyncio.fixture
async def traced_client(app: FastAPI, record: Recording) -> AsyncIterator[AsyncClient]:
    """The ordinary application, with a recorder installed for the duration."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as http:
        yield http


# ------------------------------------------------------------- the API span


async def test_a_request_produces_one_server_span(
    traced_client: AsyncClient,
    record: Recording,
) -> None:
    response = await traced_client.get("/health")

    assert response.status_code == 200
    served = record.named("GET /health")
    assert served.kind is SpanKind.SERVER
    assert served.attributes is not None
    assert served.attributes[HTTP_METHOD] == "GET"
    assert served.attributes[HTTP_ROUTE] == "/health"
    assert served.attributes[HTTP_STATUS] == 200


async def test_the_span_is_named_for_the_route_template(
    traced_client: AsyncClient,
    record: Recording,
) -> None:
    """The whole reason this middleware exists rather than the SDK's own.

    An identifier in a span name is both a cardinality problem and a
    disclosure: span names are what a trace backend indexes and displays.
    """
    lead = uuid.uuid4()

    await traced_client.get(f"/api/v1/leads/{lead}")

    names = record.names()
    # The route's own path, without the router prefix - which is exactly what
    # the metrics middleware labels with, so the two agree.
    assert "GET /leads/{lead_id}" in names
    assert not any(str(lead) in name for name in names)


async def test_a_path_that_matched_no_route_collapses_into_one_span_name(
    traced_client: AsyncClient,
    record: Recording,
) -> None:
    """Otherwise a scanner writes the span-name index."""
    await traced_client.get("/api/v1/nothing-here-at-all")
    await traced_client.get("/api/v1/nor-here")

    assert record.names().count(f"GET {UNMATCHED}") == 2


async def test_the_scrape_and_the_liveness_probe_are_not_traced(
    traced_client: AsyncClient,
    record: Recording,
) -> None:
    """An orchestrator's probe would otherwise be the commonest trace there is."""
    await traced_client.get("/health/live")
    await traced_client.get("/metrics")

    assert record.names() == []


async def test_no_authorization_header_reaches_a_span(
    traced_client: AsyncClient,
    record: Recording,
) -> None:
    """Headers are not captured at all, which is the only safe default."""
    canary = "Bearer SECRET-JWT-CANARY-eyJhbGciOiJIUzI1NiJ9"

    await traced_client.get("/health", headers={"Authorization": canary, "Cookie": canary})

    document = "\n".join(item.to_json() for item in record.finished())
    assert "SECRET-JWT-CANARY" not in document
    assert "authorization" not in document.lower()


async def test_a_query_string_never_reaches_a_span(
    traced_client: AsyncClient,
    record: Recording,
) -> None:
    """A query string carries filters, addresses and search terms."""
    await traced_client.get("/health?email=customer-canary@example.test&q=secret-search")

    document = "\n".join(item.to_json() for item in record.finished())
    assert "customer-canary@example.test" not in document
    assert "secret-search" not in document


async def test_an_inbound_traceparent_is_not_honoured(
    traced_client: AsyncClient,
    record: Recording,
) -> None:
    """A stranger does not get to choose Wasla's trace identifiers.

    Honouring this would let anybody merge unrelated requests into one trace
    and write up to 512 bytes of `tracestate` into every span they produced.
    """
    upstream = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"

    await traced_client.get("/health", headers={"traceparent": upstream})

    served = record.named("GET /health")
    assert served.context is not None
    assert format(served.context.trace_id, "032x") != "a" * 32
    assert served.parent is None


# -------------------------------------------------------- the whole chain


async def test_one_trace_spans_request_queue_worker_and_provider(
    traced_client: AsyncClient,
    record: Recording,
) -> None:
    """The drill, in one process: API to queue to worker to provider.

    Written as a test rather than only as a manual exercise, because "the
    causal chain is intact" is exactly the property that breaks silently when
    somebody changes how a job is encoded.
    """
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)

    with span("api.request", kind=SpanKind.SERVER) as request:
        await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION))
    envelope = JobEnvelope.decode(redis.lists[queue.namespace + ":pending"][0])

    with job_span(job_type="agent", envelope=envelope):
        await ProviderCall(provider=Provider.OPENAI, operation="respond").record(
            CallOutcome.SUCCESS
        )
        await ProviderCall(provider=Provider.WHATSAPP, operation="send_message").record(
            CallOutcome.SUCCESS
        )

    assert request.get_span_context() is not None
    root = record.named("api.request")
    assert root.context is not None
    expected = root.context.trace_id
    chain = {
        "queue.publish agent",
        "worker.agent",
        "provider.openai.respond",
        "provider.whatsapp.send_message",
    }
    for name in chain:
        item = record.named(name)
        assert item.context is not None
        assert item.context.trace_id == expected, f"{name} started a new trace"

    attempt = record.named("worker.agent")
    assert attempt.context is not None
    for name in ("provider.openai.respond", "provider.whatsapp.send_message"):
        provider_span = record.named(name)
        assert provider_span.parent is not None
        assert provider_span.parent.span_id == attempt.context.span_id
        assert provider_span.kind is SpanKind.CLIENT


# --------------------------------------------------- the exporter is down


async def test_an_exporter_that_raises_does_not_fail_a_request(app: FastAPI) -> None:
    """The property that decides whether this is safe to switch on."""
    exporter = ExplodingExporter()
    tracing.install_tracing(exporter, service_name="wasla-test")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://wasla.test",
        ) as http:
            for _ in range(5):
                response = await http.get("/health")
                assert response.status_code == 200
        # Force the batch out on this thread's request, so the failure has
        # definitely been attempted rather than merely queued.
        provider = tracing._provider
        assert provider is not None
        provider.force_flush()
    finally:
        tracing.shutdown_tracing()

    assert exporter.attempts >= 1, "the exporter was never called; this proves nothing"


async def test_an_exporter_that_raises_does_not_fail_a_job() -> None:
    exporter = ExplodingExporter()
    tracing.install_tracing(exporter, service_name="wasla-test")
    ran = 0
    try:
        for attempt in range(1, 4):
            envelope = JobEnvelope(
                body="payload",
                attempt=attempt,
                enqueued_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            )
            with job_span(job_type="agent", envelope=envelope):
                ran += 1
        provider = tracing._provider
        assert provider is not None
        provider.force_flush()
    finally:
        tracing.shutdown_tracing()

    assert ran == 3
    assert exporter.attempts >= 1


async def test_an_exporter_that_raises_does_not_change_a_provider_outcome() -> None:
    """A payment settles the same whether or not its span was exported."""
    exporter = ExplodingExporter()
    tracing.install_tracing(exporter, service_name="wasla-test")
    try:
        call = ProviderCall(provider=Provider.PAYMOB, operation="checkout")
        await call.record(CallOutcome.SUCCESS)
        provider = tracing._provider
        assert provider is not None
        provider.force_flush()
    finally:
        tracing.shutdown_tracing()

    assert exporter.attempts >= 1


# ------------------------------------------------------------- switching it on


async def test_tracing_off_is_the_default_and_costs_nothing() -> None:
    """No provider, no exporter, no thread, and every span a no-op."""
    tracing.shutdown_tracing()
    settings = Settings(_env_file=None, environment="test")

    configure_tracing(settings, service_name="wasla-api")

    assert not tracing.tracing_enabled()
    with span("api.request") as inactive:
        assert not inactive.get_span_context().is_valid


async def test_tracing_on_without_an_endpoint_refuses_to_start() -> None:
    """Silently exporting nothing is the failure a deployment discovers late.

    Settings refuses this combination first; `configure_tracing` refuses it
    again, because the API's lifespan is what a container's start-up actually
    runs and a second guard there costs one comparison.
    """
    with pytest.raises(ValueError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        Settings(_env_file=None, environment="test", tracing_enabled=True)

    with pytest.raises(TracingConfigurationError):
        configure_tracing(
            Settings.model_construct(
                tracing_enabled=True,
                otel_exporter_otlp_endpoint=None,
                otel_service_name=None,
                otel_traces_sampler_arg=1.0,
            ),
            service_name="wasla-api",
        )


@pytest.mark.parametrize(
    "endpoint",
    ["collector:4318", "grpc://collector:4317", "http://collector:4318/v1/traces"],
)
async def test_a_collector_address_that_could_never_work_is_refused(endpoint: str) -> None:
    """A bare host, a scheme this exporter does not speak, and a doubled path."""
    with pytest.raises(ValueError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        Settings(
            _env_file=None,
            environment="test",
            tracing_enabled=True,
            otel_exporter_otlp_endpoint=endpoint,
        )


async def test_a_usable_collector_address_is_accepted() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        tracing_enabled=True,
        otel_exporter_otlp_endpoint="http://collector:4318",
    )

    assert settings.otel_traces_sampler_arg == 1.0


async def test_sampling_nothing_still_runs_the_work() -> None:
    """A deployment that sampled everything out must still serve traffic."""
    recording, _ = install(sample_ratio=0.0)
    try:
        with span("api.request") as sampled_out:
            assert not sampled_out.is_recording()
    finally:
        tracing.shutdown_tracing()

    assert recording.finished() == []
