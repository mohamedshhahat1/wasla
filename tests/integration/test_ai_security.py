"""Adversarial tests for the AI subsystem's authority boundary.

The premise throughout: **the model is compromised and follows the attacker.**
Nothing here tries to establish that a prompt is resistant to persuasion — no
test can hold that property, and treating the model's cooperation as a control
is the mistake this file exists to rule out. What is asserted instead is that a
model doing its worst cannot exceed the authority the server built for it.

Three properties, each with its own section below:

- **Audit.** Every mutation an agent performs leaves a row naming the agent as
  actor, in the calling workspace, and a mutation that did *not* happen leaves
  none. The model cannot influence who the row says acted, because identity is
  never an argument.
- **Model policy.** A workspace cannot point an agent at a model the deployment
  will not pay for, and cannot buy unbounded output per call.
- **Retrieval limits.** A model-supplied result count is clamped before it
  reaches SQL.

`test_auth_hardening.py` holds the tool-grant half of the boundary. This file
assumes it and tests what happens on the far side of a *granted* tool.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import (
    HANDOFF_DEFINITION,
    RECORD_LEAD_DEFINITION,
    SCHEDULE_FOLLOW_UP_DEFINITION,
    ToolArgumentError,
    ToolContext,
    build_default_registry,
    validate_arguments,
)
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.billing import BillingInterval, LimitKey, Plan, SubscriptionStatus
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
)
from app.db.models.knowledge import (
    Document,
    DocumentChunk,
    DocumentSource,
    DocumentStatus,
    KnowledgeBase,
)
from app.db.models.lead import Lead
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.billing_repository import SubscriptionRepository
from app.services.agent_service import AgentService
from app.services.retrieval_service import DEFAULT_TOP_K, MAX_TOP_K, RetrievalService
from app.services.usage_service import UsageRecorder
from tests.fake_embeddings import FakeEmbeddings, embed_text

pytestmark = pytest.mark.integration

ALLOWED_MODEL = "gpt-4.1-mini"
SECOND_ALLOWED_MODEL = "gpt-4.1"
FORBIDDEN_MODEL = "o1-pro"


async def _tenant(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _conversation(session: AsyncSession, *, tenant: Tenant) -> Conversation:
    suffix = uuid.uuid4().hex[:8]
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{suffix}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"2010{suffix}")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
        mode=ConversationMode.AI,
    )
    session.add(conversation)
    await session.flush()
    return conversation


def _context(session: AsyncSession, *, tenant: Tenant, conversation: Conversation) -> ToolContext:
    """The context an orchestrator builds — server-side, never model-supplied."""
    return ToolContext(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        session=session,
        embeddings=None,
    )


async def _audit(session: AsyncSession, action: AuditAction | None = None) -> list[AuditLog]:
    statement = select(AuditLog)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    return list((await session.scalars(statement)).all())


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "rate_limit_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ============================================================ audit trail


async def test_a_handoff_by_the_agent_is_recorded_against_the_agent(
    db_session: AsyncSession,
) -> None:
    """The archetypal question: a conversation moved and nobody typed anything.

    The actor kind is the assertion that matters. A row saying `system` would
    put an agent's decision in the same bucket as the billing sweep's, and
    `user` would attribute it to a colleague who did nothing.
    """
    tenant = await _tenant(db_session, slug="handoff")
    conversation = await _conversation(db_session, tenant=tenant)
    registry = build_default_registry()

    await registry.run(
        name=HANDOFF_DEFINITION.name,
        arguments={"reason": "The customer asked for a person."},
        context=_context(db_session, tenant=tenant, conversation=conversation),
    )
    await db_session.flush()

    rows = await _audit(db_session, AuditAction.AGENT_HANDOFF_REQUESTED)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.actor_kind is AuditActorKind.AGENT
    assert entry.actor_id is None
    assert entry.tenant_id == tenant.id
    assert entry.target_type == "conversation"
    assert entry.target_id == conversation.id
    assert entry.meta == {"conversation_id": str(conversation.id)}


async def test_the_handoff_reason_is_not_copied_into_the_trail(
    db_session: AsyncSession,
) -> None:
    """The reason is a sentence about a customer. It lives on the conversation.

    An audit log that accumulated message text would become a second copy of
    the conversation with a different retention story, which is a privacy
    problem rather than an accountability win.
    """
    tenant = await _tenant(db_session, slug="reason")
    conversation = await _conversation(db_session, tenant=tenant)
    secret = "customer mentioned account 4111111111111111"

    await build_default_registry().run(
        name=HANDOFF_DEFINITION.name,
        arguments={"reason": secret},
        context=_context(db_session, tenant=tenant, conversation=conversation),
    )
    await db_session.flush()

    rows = await _audit(db_session, AuditAction.AGENT_HANDOFF_REQUESTED)
    assert secret not in str(rows[0].meta)
    assert rows[0].target_label is None


async def test_recording_a_lead_names_the_fields_but_never_their_values(
    db_session: AsyncSession,
) -> None:
    """ "The agent set a phone number" is auditable; the number is not.

    Personal data belongs on the lead row, where deleting the lead deletes it.
    Copying it into an append-only trail would outlive every deletion request.
    """
    tenant = await _tenant(db_session, slug="lead")
    conversation = await _conversation(db_session, tenant=tenant)

    await build_default_registry().run(
        name=RECORD_LEAD_DEFINITION.name,
        arguments={"name": "Ahmed Farouk", "phone": "+201234567890", "budget_amount": 500000},
        context=_context(db_session, tenant=tenant, conversation=conversation),
    )
    await db_session.flush()

    rows = await _audit(db_session, AuditAction.AGENT_LEAD_RECORDED)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.actor_kind is AuditActorKind.AGENT
    assert entry.tenant_id == tenant.id
    assert entry.target_type == "lead"

    rendered = str(entry.meta)
    assert "Ahmed Farouk" not in rendered
    assert "+201234567890" not in rendered
    assert "500000" not in rendered
    assert set(entry.meta["fields"]) == {"name", "phone", "budget_amount"}

    lead = await db_session.scalar(select(Lead).where(Lead.tenant_id == tenant.id))
    assert lead is not None
    assert entry.target_id == lead.id


async def test_a_scheduled_follow_up_records_the_delay_not_the_message(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, slug="followup")
    conversation = await _conversation(db_session, tenant=tenant)
    body = "Just checking whether you decided on the 150m finishing quote."

    await build_default_registry().run(
        name=SCHEDULE_FOLLOW_UP_DEFINITION.name,
        arguments={"delay_minutes": 1440, "message": body},
        context=_context(db_session, tenant=tenant, conversation=conversation),
    )
    await db_session.flush()

    rows = await _audit(db_session, AuditAction.AGENT_FOLLOW_UP_SCHEDULED)
    assert len(rows) == 1
    assert rows[0].meta["delay_minutes"] == 1440
    assert body not in str(rows[0].meta)
    assert rows[0].actor_kind is AuditActorKind.AGENT


async def test_a_refused_argument_records_nothing(db_session: AsyncSession) -> None:
    """A tool that never ran must leave no trace saying it did.

    This is the half of the audit contract that is easy to get wrong: recording
    at the top of a handler rather than after the mutation produces a trail
    full of actions that did not happen, which is worse than no trail because
    it is believed.
    """
    tenant = await _tenant(db_session, slug="refused")
    conversation = await _conversation(db_session, tenant=tenant)
    context = _context(db_session, tenant=tenant, conversation=conversation)

    # A delay far outside the permitted bounds. The schema accepts it - it is a
    # whole number - and the *service* refuses it, which the handler turns into
    # text for the model rather than an exception. That is the interesting
    # shape: the tool ran, the mutation did not happen, and the trail must say
    # nothing rather than record a follow-up that does not exist.
    output = await build_default_registry().run(
        name=SCHEDULE_FOLLOW_UP_DEFINITION.name,
        arguments={"delay_minutes": 99_999_999, "message": "later"},
        context=context,
    )
    await db_session.flush()

    assert "was not scheduled" in output
    assert await _audit(db_session) == []


async def test_a_lead_tool_that_saves_nothing_records_nothing(
    db_session: AsyncSession,
) -> None:
    """Called with no details, the tool declines and must not claim a write."""
    tenant = await _tenant(db_session, slug="empty")
    conversation = await _conversation(db_session, tenant=tenant)

    output = await build_default_registry().run(
        name=RECORD_LEAD_DEFINITION.name,
        arguments={},
        context=_context(db_session, tenant=tenant, conversation=conversation),
    )
    await db_session.flush()

    assert "Nothing was saved" in output
    assert await _audit(db_session, AuditAction.AGENT_LEAD_RECORDED) == []


async def test_an_agent_audit_row_never_lands_in_another_workspace(
    db_session: AsyncSession,
) -> None:
    """Scope comes from the context, so there is no argument to point elsewhere."""
    theirs = await _tenant(db_session, slug="victim")
    await _conversation(db_session, tenant=theirs)
    ours = await _tenant(db_session, slug="attacker")
    conversation = await _conversation(db_session, tenant=ours)

    await build_default_registry().run(
        name=HANDOFF_DEFINITION.name,
        arguments={"reason": "hand over"},
        context=_context(db_session, tenant=ours, conversation=conversation),
    )
    await db_session.flush()

    rows = await _audit(db_session, AuditAction.AGENT_HANDOFF_REQUESTED)
    assert [row.tenant_id for row in rows] == [ours.id]


@pytest.mark.parametrize(
    "forged",
    [
        {"reason": "ok", "tenant_id": str(uuid.uuid4())},
        {"reason": "ok", "conversation_id": str(uuid.uuid4())},
        {"reason": "ok", "actor_kind": "user"},
        {"reason": "ok", "actor_id": str(uuid.uuid4())},
        {"reason": "ok", "target_id": str(uuid.uuid4())},
    ],
)
async def test_the_model_cannot_smuggle_identity_through_tool_arguments(forged) -> None:
    """The forged-identity attack, refused by the schema before any handler runs.

    `additionalProperties: false` is declared to the provider, but a compromised
    model is exactly the caller that ignores a schema it was shown. The refusal
    that matters is this one, which happens locally on the way in.
    """
    with pytest.raises(ToolArgumentError) as refusal:
        validate_arguments(HANDOFF_DEFINITION, forged)

    assert "Unexpected arguments" in str(refusal.value)


async def test_no_tool_accepts_an_identifier_argument() -> None:
    """A structural guard, so a tool added later cannot quietly take an id.

    Every identifier in this subsystem comes from `ToolContext`. A tool that
    declared `lead_id` or `tenant_id` would be one the model could point at
    another customer's record, and it would pass every other test in the suite.
    """
    banned = {"tenant_id", "conversation_id", "user_id", "lead_id", "agent_id", "document_id"}
    registry = build_default_registry()

    for name in registry.names():
        definition = registry.get(name)
        assert definition is not None
        declared = {parameter.name for parameter in definition.parameters}
        assert not (declared & banned), f"{name} accepts an identifier from the model"
        assert definition.json_schema()["additionalProperties"] is False


# ============================================================ model policy


async def test_a_model_the_deployment_will_not_run_is_refused(
    db_session: AsyncSession,
) -> None:
    """The cost boundary. A workspace cannot spend the operator's money freely."""
    tenant = await _tenant(db_session, slug="modelpolicy")
    service = AgentService(
        session=db_session,
        settings=_settings(openai_allowed_models=[ALLOWED_MODEL]),
        tenant_id=tenant.id,
    )

    with pytest.raises(ValidationError) as refusal:
        await service.create(name="Sales", system_prompt="Help.", model=FORBIDDEN_MODEL)

    assert FORBIDDEN_MODEL in str(refusal.value)
    # A configuration mistake an administrator can act on, not a 500 and not a
    # failed inference a customer waits for. This is the documented contract.
    assert refusal.value.status_code == 422


async def test_an_allowed_model_is_accepted(db_session: AsyncSession) -> None:
    """The control. A policy that refused everything would pass the test above."""
    tenant = await _tenant(db_session, slug="allowed")
    service = AgentService(
        session=db_session,
        settings=_settings(openai_allowed_models=[ALLOWED_MODEL, SECOND_ALLOWED_MODEL]),
        tenant_id=tenant.id,
    )

    agent = await service.create(name="Sales", system_prompt="Help.", model=SECOND_ALLOWED_MODEL)

    assert agent.model == SECOND_ALLOWED_MODEL


async def test_the_configured_default_is_always_permitted(db_session: AsyncSession) -> None:
    """An allowlist omitting the fallback would make an ordinary agent unbuildable."""
    tenant = await _tenant(db_session, slug="default")
    service = AgentService(
        session=db_session,
        settings=_settings(
            openai_model=ALLOWED_MODEL,
            openai_allowed_models=[SECOND_ALLOWED_MODEL],
        ),
        tenant_id=tenant.id,
    )

    agent = await service.create(name="Sales", system_prompt="Help.")

    assert agent.model == ALLOWED_MODEL


async def test_an_empty_allowlist_restricts_nothing(db_session: AsyncSession) -> None:
    """The development default, stated so it cannot change by accident."""
    tenant = await _tenant(db_session, slug="unrestricted")
    service = AgentService(
        session=db_session,
        settings=_settings(openai_allowed_models=[]),
        tenant_id=tenant.id,
    )

    agent = await service.create(name="Sales", system_prompt="Help.", model=FORBIDDEN_MODEL)

    assert agent.model == FORBIDDEN_MODEL


async def test_a_model_cannot_be_switched_by_update(db_session: AsyncSession) -> None:
    """The update path is the one an attacker with admin reaches for second."""
    tenant = await _tenant(db_session, slug="switch")
    service = AgentService(
        session=db_session,
        settings=_settings(openai_allowed_models=[ALLOWED_MODEL]),
        tenant_id=tenant.id,
    )
    agent = await service.create(name="Sales", system_prompt="Help.")

    with pytest.raises(ValidationError):
        await service.update(agent.id, model=FORBIDDEN_MODEL)

    await db_session.refresh(agent)
    assert agent.model == ALLOWED_MODEL


async def test_output_tokens_above_the_ceiling_are_refused(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="ceiling")
    service = AgentService(
        session=db_session,
        settings=_settings(openai_max_output_tokens=1_024),
        tenant_id=tenant.id,
    )

    with pytest.raises(ValidationError) as refusal:
        await service.create(name="Sales", system_prompt="Help.", max_output_tokens=4_096)

    assert "1024" in str(refusal.value).replace(",", "")
    assert refusal.value.status_code == 422


async def test_omitting_output_tokens_takes_the_configured_ceiling(
    db_session: AsyncSession,
) -> None:
    """Null used to mean "whatever the provider decides", which is unbounded spend."""
    tenant = await _tenant(db_session, slug="defaulted")
    service = AgentService(
        session=db_session,
        settings=_settings(openai_max_output_tokens=1_024),
        tenant_id=tenant.id,
    )

    agent = await service.create(name="Sales", system_prompt="Help.")

    assert agent.max_output_tokens == 1_024


async def test_the_ceiling_is_enforced_on_update_too(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="ceilingupdate")
    service = AgentService(
        session=db_session,
        settings=_settings(openai_max_output_tokens=1_024),
        tenant_id=tenant.id,
    )
    agent = await service.create(name="Sales", system_prompt="Help.")

    with pytest.raises(ValidationError):
        await service.update(agent.id, max_output_tokens=8_192)


async def test_model_configuration_is_per_workspace(db_session: AsyncSession) -> None:
    """One workspace's agent cannot be reconfigured through another's service."""
    ours = await _tenant(db_session, slug="ours")
    theirs = await _tenant(db_session, slug="theirs")
    settings = _settings(openai_allowed_models=[ALLOWED_MODEL])

    mine = AgentService(session=db_session, settings=settings, tenant_id=ours.id)
    agent = await mine.create(name="Sales", system_prompt="Help.")

    intruder = AgentService(session=db_session, settings=settings, tenant_id=theirs.id)
    with pytest.raises(Exception):  # noqa: B017 - NotFoundError or TenantIsolationError
        await intruder.update(agent.id, system_prompt="Owned.")


async def test_no_tool_can_change_the_model_or_the_token_ceiling() -> None:
    """Model choice is configuration, and configuration is not a tool argument."""
    registry = build_default_registry()
    banned = {"model", "max_output_tokens", "temperature", "system_prompt"}

    for name in registry.names():
        definition = registry.get(name)
        assert definition is not None
        declared = {parameter.name for parameter in definition.parameters}
        assert not (declared & banned), f"{name} exposes model configuration"


# ============================================================ retrieval limits


SEEDED_CHUNKS = MAX_TOP_K * 3
CHUNK_SUBJECT = "the finishing quote covers plaster paint and flooring"


async def _seed_chunks(session: AsyncSession, *, tenant: Tenant) -> None:
    """Enough near-identical chunks that an unclamped LIMIT would return more.

    Without real rows the clamp cannot be observed: an empty knowledge base
    returns nothing whether the limit is 10 or 10,000, so a test written
    against one would pass with the clamp deleted. They are deliberately
    similar to each other so every one of them clears the distance threshold
    and the only thing deciding the count is the limit.
    """
    base = KnowledgeBase(tenant_id=tenant.id, name="General")
    session.add(base)
    await session.flush()

    document = Document(
        tenant_id=tenant.id,
        knowledge_base_id=base.id,
        title="Quotes",
        source=DocumentSource.TEXT,
        status=DocumentStatus.READY,
        content_hash=uuid.uuid4().hex,
        byte_size=0,
    )
    session.add(document)
    await session.flush()

    for ordinal in range(SEEDED_CHUNKS):
        text = f"{CHUNK_SUBJECT} number {ordinal}"
        session.add(
            DocumentChunk(
                tenant_id=tenant.id,
                document_id=document.id,
                knowledge_base_id=base.id,
                ordinal=ordinal,
                content=text,
                token_estimate=len(text) // 4,
                embedding=embed_text(text),
            )
        )
    await session.flush()


@pytest.mark.parametrize(
    "requested",
    [-1_000_000, -1, 0, 1, DEFAULT_TOP_K, MAX_TOP_K, MAX_TOP_K + 1, 1_000, 1_000_000],
)
async def test_a_model_supplied_result_count_is_always_clamped(
    db_session: AsyncSession,
    requested: int,
) -> None:
    """`max_results` is the one number a model chooses that reaches SQL.

    Unclamped it becomes the `LIMIT` of a vector scan, which is a query cost an
    attacker picks. The bound is asserted from both ends: never above
    `MAX_TOP_K`, and never negative or zero, which would be either an error or
    an unbounded scan depending on the dialect.
    """
    tenant = await _tenant(db_session, slug="clamp")
    await _seed_chunks(db_session, tenant=tenant)
    service = RetrievalService(
        session=db_session,
        tenant_id=tenant.id,
        embeddings=FakeEmbeddings(),
    )

    retrieval = await service.search(query=CHUNK_SUBJECT, top_k=requested)

    assert len(retrieval.passages) <= MAX_TOP_K
    # And the clamp is a floor as well as a ceiling: a hostile zero or negative
    # must still return the one best passage rather than nothing or everything.
    assert len(retrieval.passages) >= 1


async def test_the_clamp_actually_bites_on_a_full_knowledge_base(
    db_session: AsyncSession,
) -> None:
    """The control for the parametrised test above.

    If seeding were broken, every case there would pass against an empty index
    and prove nothing. This asserts the corpus really is larger than the cap,
    so `<= MAX_TOP_K` is a limit being enforced rather than a shortage of rows.
    """
    tenant = await _tenant(db_session, slug="bites")
    await _seed_chunks(db_session, tenant=tenant)
    service = RetrievalService(
        session=db_session,
        tenant_id=tenant.id,
        embeddings=FakeEmbeddings(),
    )

    retrieval = await service.search(query=CHUNK_SUBJECT, top_k=1_000_000)

    assert SEEDED_CHUNKS > MAX_TOP_K
    assert len(retrieval.passages) == MAX_TOP_K


class _CountingEmbeddings:
    """Counts calls, so "no provider call" can be asserted rather than assumed."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_one(self, text: str) -> list[float]:
        self.calls += 1
        return embed_text(text)


async def test_an_empty_query_never_reaches_the_provider(db_session: AsyncSession) -> None:
    """A blank search must not buy an embedding call."""
    tenant = await _tenant(db_session, slug="blank")
    embeddings = _CountingEmbeddings()
    service = RetrievalService(
        session=db_session,
        tenant_id=tenant.id,
        embeddings=embeddings,  # type: ignore[arg-type]
    )

    retrieval = await service.search(query="   ")

    assert retrieval.is_empty
    assert embeddings.calls == 0


# ============================================================ allowance bound


class _SessionHandle:
    """Hands the worker the test's own session, so its writes roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


class _NullRedis:
    @property
    def client(self):
        return object()


async def _plan_with_ai_limit(session: AsyncSession, *, tenant: Tenant, limit: int) -> None:
    """A workspace on a plan that permits exactly `limit` AI requests."""
    plan = Plan(
        code=f"cap-{uuid.uuid4().hex[:8]}",
        name="Capped",
        price=Decimal("10.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        limits={LimitKey.PERIOD_AI_REQUESTS.value: limit},
    )
    session.add(plan)
    await session.flush()
    now = datetime.now(UTC)
    SubscriptionRepository(session, tenant_id=tenant.id).create(
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now - timedelta(days=5),
        current_period_end=now + timedelta(days=25),
    )
    await session.flush()


async def test_a_turn_cannot_spend_more_rounds_than_the_allowance_permits(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overspend this fix exists to close.

    One agent turn is up to `MAX_ROUNDS` provider calls and each is metered as
    a request. A worker that only asked "may I make one?" would then knowingly
    make three, billing a workspace past a limit the system had already read.

    The allowance here permits three requests and two are already spent, so the
    turn may make exactly one call. The assertion is on the budget the worker
    hands the orchestrator, because that is where the decision is taken.
    """
    from app.workers import ai_worker as worker_module
    from app.workers.queue import AgentJob

    tenant = await _tenant(db_session, slug="allowance")
    conversation = await _conversation(db_session, tenant=tenant)
    await _plan_with_ai_limit(db_session, tenant=tenant, limit=3)

    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(UsageEventType.AI_REQUEST, quantity=2)
    await db_session.flush()

    seen: dict[str, int] = {}

    class _Capturing:
        def __init__(self, **kwargs: object) -> None:
            seen["max_rounds"] = int(kwargs["max_rounds"])  # type: ignore[arg-type]

        async def answer(self, **_: object):
            from app.agents.orchestrator import AgentOutcome
            from app.integrations.openai.types import TokenUsage

            return AgentOutcome(
                reply=None,
                handed_off=False,
                tools_run=(),
                usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0),
                rounds=1,
            )

    monkeypatch.setattr(worker_module, "AgentOrchestrator", _Capturing)
    monkeypatch.setattr(
        worker_module,
        "build_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ),
    )

    worker = worker_module.AgentWorker(
        database=_SessionHandle(db_session),
        redis=_NullRedis(),
        settings=_settings(openai_api_key="test-key-not-a-real-credential"),
    )
    await worker._handle(AgentJob(tenant_id=tenant.id, conversation_id=conversation.id))

    assert seen["max_rounds"] == 1


async def test_an_unlimited_plan_gets_the_full_round_budget(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the bound must not throttle a workspace that has no limit."""
    from app.agents.orchestrator import MAX_ROUNDS
    from app.workers import ai_worker as worker_module
    from app.workers.queue import AgentJob

    tenant = await _tenant(db_session, slug="unlimited")
    conversation = await _conversation(db_session, tenant=tenant)

    seen: dict[str, int] = {}

    class _Capturing:
        def __init__(self, **kwargs: object) -> None:
            seen["max_rounds"] = int(kwargs["max_rounds"])  # type: ignore[arg-type]

        async def answer(self, **_: object):
            from app.agents.orchestrator import AgentOutcome
            from app.integrations.openai.types import TokenUsage

            return AgentOutcome(
                reply=None,
                handed_off=False,
                tools_run=(),
                usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0),
                rounds=1,
            )

    monkeypatch.setattr(worker_module, "AgentOrchestrator", _Capturing)
    monkeypatch.setattr(
        worker_module,
        "build_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ),
    )

    worker = worker_module.AgentWorker(
        database=_SessionHandle(db_session),
        redis=_NullRedis(),
        settings=_settings(openai_api_key="test-key-not-a-real-credential"),
    )
    await worker._handle(AgentJob(tenant_id=tenant.id, conversation_id=conversation.id))

    # No subscription at all means no limit for this key, which is the
    # deployment default and must not be read as "zero allowed".
    assert seen["max_rounds"] == MAX_ROUNDS
