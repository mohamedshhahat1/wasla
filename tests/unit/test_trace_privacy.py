"""What a trace is allowed to say, held against what it actually says.

A trace backend is a third party. Whoever operates it can read every span name,
every attribute and every status message this application exports, for every
request, indefinitely, in a system that has none of Wasla's tenant isolation.
That makes an attribute a *disclosure decision*, not a debugging convenience,
and this file is where the decision is enforced rather than remembered.

The method is deliberately two-sided. An allowlist test alone would pass on
code that exports nothing; a canary test alone would pass on code that exports
a field this suite happened not to name. So:

**The allowlist.** Every attribute on every span produced by a realistic flow
is checked against `ALLOWED_ATTRIBUTES`. A new attribute fails here whatever it
is called, which is what makes this survive somebody adding
`span.set_attribute("tenant_id", ...)` "just for debugging".

**The canaries.** Distinctive values are pushed through the parts of the system
that handle secrets, credentials and customer content, and the exported spans
are searched for them verbatim. A canary that appears is a leak whether or not
anybody predicted the field it arrived in.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from app.core.telemetry import CallOutcome, Provider, ProviderCall
from app.core.tracing import (
    ALLOWED_ATTRIBUTES,
    SpanKind,
    span,
)
from app.workers.dispatch import job_span
from app.workers.queue import AgentJob, AgentQueue, JobEnvelope
from tests.fake_queue_redis import FakeQueueRedis
from tests.tracing_recorder import Recording, recording_spans

# Values chosen to be findable and to be nothing else. If one of these turns up
# in an exported span, it got there from the thing named after it.
JWT_CANARY = "SECRET-JWT-CANARY-eyJhbGciOiJIUzI1NiJ9"
PAYMOB_CANARY = "SECRET-PAYMOB-CANARY-sk-live-0000"
EMAIL_CANARY = "customer-email-canary@example.test"
PHONE_CANARY = "201555000111222"
PROMPT_CANARY = "PROMPT-CANARY the customer asked about finishing a flat"
OAUTH_CANARY = "SECRET-OAUTH-CODE-CANARY-4%2F0Ab"
STORAGE_CANARY = "tenant/9f/media/SECRET-STORAGE-KEY-CANARY.bin"

CANARIES = (
    JWT_CANARY,
    PAYMOB_CANARY,
    EMAIL_CANARY,
    PHONE_CANARY,
    PROMPT_CANARY,
    OAUTH_CANARY,
    STORAGE_CANARY,
)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def record():
    yield from recording_spans()


def exported(record: Recording) -> str:
    """Everything a collector would receive, as one searchable document.

    Serialised through the SDK's own `to_json`, so this searches what is
    actually sent rather than a summary this test assembled - which is the
    difference between proving a canary is absent and proving this test did not
    look for it.
    """
    return "\n".join(item.to_json() for item in record.finished())


# ------------------------------------------------------------- the allowlist


async def test_every_attribute_on_every_span_is_on_the_allowlist(record: Recording) -> None:
    """The structural half. A new attribute fails here whatever it is named."""
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    with span("api.request", kind=SpanKind.SERVER):
        await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION))
    envelope = JobEnvelope.decode(redis.lists[queue.namespace + ":pending"][0])
    with job_span(job_type="agent", envelope=envelope):
        await ProviderCall(provider=Provider.OPENAI, operation="respond").record(
            CallOutcome.SUCCESS
        )

    seen: set[str] = set()
    for item in record.finished():
        seen.update(item.attributes or {})

    assert seen, "the flow produced no attributes at all; this test proves nothing"
    assert (
        seen <= ALLOWED_ATTRIBUTES
    ), f"undeclared span attributes: {sorted(seen - ALLOWED_ATTRIBUTES)}"


async def test_the_allowlist_names_no_identifier() -> None:
    """Read the other way round: the list itself must contain nothing personal.

    A guard whose allowlist quietly grew a `tenant_id` entry would pass the
    test above for ever.
    """
    forbidden = (
        "tenant",
        "workspace",
        "user",
        "conversation",
        "contact",
        "lead",
        "invoice",
        "payment",
        "media",
        "message",
        "phone",
        "email",
        "token",
        "prompt",
        "body",
    )
    for attribute in ALLOWED_ATTRIBUTES:
        assert not any(word in attribute for word in forbidden), attribute


# --------------------------------------------------------------- the canaries


async def test_a_provider_span_carries_no_payload(record: Recording) -> None:
    """The prompt, the answer and the tool arguments are all absent."""
    call = ProviderCall(provider=Provider.OPENAI, operation="respond")
    await call.record(CallOutcome.SUCCESS)

    document = exported(record)
    assert "provider.openai.respond" in document
    for canary in CANARIES:
        assert canary not in document


async def test_a_failed_provider_span_carries_the_outcome_not_the_reason(
    record: Recording,
) -> None:
    """A provider's error text is unbounded and echoes the request."""
    call = ProviderCall(provider=Provider.PAYMOB, operation="checkout")
    await call.record(CallOutcome.FAILURE)

    document = exported(record)
    assert '"failure"' in document
    assert PAYMOB_CANARY not in document


async def test_an_exception_message_never_reaches_a_span(record: Recording) -> None:
    """The default the `span` helper exists to turn off.

    With `record_exception` and `set_status_on_exception` left on - which is
    how the SDK ships - this exception's text and a full stack trace would both
    be exported.
    """
    with pytest.raises(ValueError), span("worker.agent", kind=SpanKind.CONSUMER):
        raise ValueError(f"the customer said {PROMPT_CANARY} and the card was {PAYMOB_CANARY}")

    document = exported(record)
    assert PROMPT_CANARY not in document
    assert PAYMOB_CANARY not in document
    # The class name survives, which is what an operator can act on.
    assert "ValueError" in document
    assert "exception" not in document.lower() or "ValueError" in document


async def test_a_failing_span_is_marked_an_error(record: Recording) -> None:
    """The status still says something went wrong, without saying what."""
    with pytest.raises(RuntimeError), span("worker.agent", kind=SpanKind.CONSUMER):
        raise RuntimeError("unimportant detail")

    failed = record.named("worker.agent")
    assert failed.status.status_code.name == "ERROR"
    assert failed.status.description == "RuntimeError"


async def test_a_span_records_no_exception_event(record: Recording) -> None:
    """Events are where a stack trace would arrive, so there are none."""
    with pytest.raises(ValueError), span("worker.agent"):
        raise ValueError(PROMPT_CANARY)

    failed = record.named("worker.agent")
    assert list(failed.events) == []


async def test_a_queue_span_carries_no_job_payload(record: Recording) -> None:
    """The envelope's body names a tenant and a conversation. The span does not."""
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)

    with span("api.request", kind=SpanKind.SERVER):
        await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION))

    document = exported(record)
    assert str(TENANT) not in document
    assert str(CONVERSATION) not in document


async def test_a_worker_span_carries_no_job_payload(record: Recording) -> None:
    envelope = JobEnvelope(
        body=json.dumps(
            {
                "tenant_id": str(TENANT),
                "conversation_id": str(CONVERSATION),
                "note": PROMPT_CANARY,
            }
        ),
        attempt=2,
        enqueued_at=datetime.now(UTC),
    )

    with job_span(job_type="agent", envelope=envelope):
        pass

    document = exported(record)
    assert str(TENANT) not in document
    assert PROMPT_CANARY not in document


async def test_no_span_name_carries_an_identifier(record: Recording) -> None:
    """Span names are what a backend groups by, so they are bounded too."""
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION))
    envelope = JobEnvelope.decode(redis.lists[queue.namespace + ":pending"][0])
    with job_span(job_type="agent", envelope=envelope):
        await ProviderCall(provider=Provider.WHATSAPP, operation="send_message").record(
            CallOutcome.SUCCESS
        )

    for name in record.names():
        assert str(TENANT) not in name
        assert str(CONVERSATION) not in name
        for canary in CANARIES:
            assert canary not in name


# ------------------------------------------------- what the resource exports


async def test_the_resource_names_the_service_and_not_the_machine(record: Recording) -> None:
    """A trace backend learns which service sent a span, and no more than that.

    OpenTelemetry's host, process and container detectors would add a hostname,
    an IP address, a process id and the command line. They are opt-in and are
    deliberately not opted into: a deployment's topology is not something to
    hand a third party by default. `service.instance.id` is a UUID generated
    per process, which distinguishes replicas without naming any of them.
    """
    with span("api.request"):
        pass

    resource = record.named("api.request").resource
    assert resource.attributes["service.name"] == "wasla-test"
    for attribute in resource.attributes:
        assert not attribute.startswith(("host.", "process.", "container.", "os."))
