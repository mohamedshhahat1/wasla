"""The template registry against real PostgreSQL.

What needs a database here is identity. A sync must recognise the template it
already stored — matched on name and language within an account — and update it
rather than insert a second row, and that is a unique constraint doing the work,
not a service. Withdrawal is the same story from the other side: a template that
vanishes from Meta keeps its row so a follow-up referencing it still resolves.

Meta is replaced by a stub returning its documented shapes. What is under test
is what the registry concludes, not whether httpx works.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import TenantIsolationError, ValidationError
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
)
from app.db.models.follow_up import FollowUpStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.services.follow_up_service import FollowUpService
from app.services.template_service import WITHDRAWN_REASON, TemplateService

pytestmark = pytest.mark.integration

SETTINGS = Settings(
    _env_file=None,
    environment="test",
    log_format="console",
    log_level="WARNING",
    cors_origins=[],
    meta_access_token="test-access-token",
)


class StubMeta:
    """Answers with whatever template payloads a test scripted."""

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.calls: list[str] = []

    async def list_templates(self, *, waba_id: str, **_: Any) -> list[dict[str, Any]]:
        self.calls.append(waba_id)
        return self.payloads


def _payload(
    name: str = "order_update",
    *,
    language: str = "ar_EG",
    status: str = "APPROVED",
    category: str = "MARKETING",
    body: str | None = "Hello {{1}}, your order {{2}} is ready.",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"meta-{name}",
        "name": name,
        "language": language,
        "status": status,
        "category": category,
    }
    if body is not None:
        payload["components"] = [{"type": "BODY", "text": body}]
    payload.update(extra)
    return payload


async def _tenant(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _account(session: AsyncSession, *, tenant: Tenant, suffix: str = "a") -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{tenant.slug}-{suffix}",
        waba_id=f"waba-{tenant.slug}-{suffix}",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()
    return account


def _service(
    session: AsyncSession, *, tenant: Tenant, meta: StubMeta | None = None
) -> TemplateService:
    return TemplateService(
        session=session,
        tenant_id=tenant.id,
        settings=SETTINGS,
        whatsapp=meta,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ syncing


async def test_a_sync_writes_down_what_meta_says(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="sync-writes")
    account = await _account(db_session, tenant=tenant)
    meta = StubMeta(_payload())

    outcome = await _service(db_session, tenant=tenant, meta=meta).sync(account.id)
    await db_session.flush()

    assert (outcome.created, outcome.updated, outcome.withdrawn) == (1, 0, 0)
    assert meta.calls == [account.waba_id]

    stored = await WhatsAppTemplateRepository(db_session, tenant_id=tenant.id).get_by_name(
        account_id=account.id,
        name="order_update",
        language="ar_EG",
    )
    assert stored is not None
    assert stored.status is TemplateStatus.APPROVED
    assert stored.category is TemplateCategory.MARKETING
    assert stored.meta_template_id == "meta-order_update"
    assert stored.variable_count == 2
    assert stored.body_text is not None and "{{1}}" in stored.body_text
    assert stored.synced_at is not None


async def test_syncing_twice_updates_the_same_row(db_session: AsyncSession) -> None:
    """Identity is name and language within an account, which is Meta's own."""
    tenant = await _tenant(db_session, slug="sync-twice")
    account = await _account(db_session, tenant=tenant)

    await _service(db_session, tenant=tenant, meta=StubMeta(_payload())).sync(account.id)
    await db_session.flush()

    second = await _service(
        db_session,
        tenant=tenant,
        meta=StubMeta(_payload(status="PAUSED")),
    ).sync(account.id)
    await db_session.flush()

    assert (second.created, second.updated) == (0, 1)
    rows = await WhatsAppTemplateRepository(db_session, tenant_id=tenant.id).list_for_account(
        account.id
    )
    assert len(rows) == 1
    assert rows[0].status is TemplateStatus.PAUSED


async def test_the_same_name_in_two_languages_is_two_templates(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="two-languages")
    account = await _account(db_session, tenant=tenant)
    meta = StubMeta(_payload(language="ar_EG"), _payload(language="en_US"))

    outcome = await _service(db_session, tenant=tenant, meta=meta).sync(account.id)
    await db_session.flush()

    assert outcome.created == 2


async def test_a_template_that_vanished_from_meta_is_disabled_not_deleted(
    db_session: AsyncSession,
) -> None:
    """A campaign may reference it, and the reason it stopped working matters."""
    tenant = await _tenant(db_session, slug="withdrawn")
    account = await _account(db_session, tenant=tenant)

    await _service(db_session, tenant=tenant, meta=StubMeta(_payload("gone"))).sync(account.id)
    await db_session.flush()

    outcome = await _service(db_session, tenant=tenant, meta=StubMeta()).sync(account.id)
    await db_session.flush()

    assert outcome.withdrawn == 1
    rows = await WhatsAppTemplateRepository(db_session, tenant_id=tenant.id).list_for_account(
        account.id
    )
    assert len(rows) == 1
    assert rows[0].status is TemplateStatus.DISABLED
    assert rows[0].rejection_reason == WITHDRAWN_REASON


async def test_a_template_with_no_name_is_skipped_rather_than_stored(
    db_session: AsyncSession,
) -> None:
    """Nothing addressable, so nothing a send could ever use."""
    tenant = await _tenant(db_session, slug="nameless")
    account = await _account(db_session, tenant=tenant)
    meta = StubMeta({"id": "x", "language": "ar_EG", "status": "APPROVED"}, _payload())

    outcome = await _service(db_session, tenant=tenant, meta=meta).sync(account.id)
    await db_session.flush()

    assert outcome.created == 1


async def test_a_status_meta_invents_later_lands_unsendable(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="new-status")
    account = await _account(db_session, tenant=tenant)
    meta = StubMeta(_payload(status="SOMETHING_NEW", category="ALSO_NEW"))

    await _service(db_session, tenant=tenant, meta=meta).sync(account.id)
    await db_session.flush()

    rows = await WhatsAppTemplateRepository(db_session, tenant_id=tenant.id).list_for_account(
        account.id
    )
    assert rows[0].status is TemplateStatus.UNKNOWN
    assert rows[0].category is TemplateCategory.UNKNOWN
    assert rows[0].is_sendable is False


async def test_a_rejection_reason_is_kept_and_none_is_not(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="rejected")
    account = await _account(db_session, tenant=tenant)
    meta = StubMeta(
        _payload("refused", status="REJECTED", rejected_reason="INVALID_FORMAT"),
        _payload("fine", rejected_reason="NONE"),
    )

    await _service(db_session, tenant=tenant, meta=meta).sync(account.id)
    await db_session.flush()

    rows = {
        row.name: row
        for row in await WhatsAppTemplateRepository(
            db_session, tenant_id=tenant.id
        ).list_for_account(account.id)
    }
    assert rows["refused"].rejection_reason == "INVALID_FORMAT"
    assert rows["fine"].rejection_reason is None


async def test_a_quality_score_is_read_from_either_shape(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="quality")
    account = await _account(db_session, tenant=tenant)
    meta = StubMeta(
        _payload("scored", quality_score={"score": "GREEN"}),
        _payload("rated", quality_rating="YELLOW"),
    )

    await _service(db_session, tenant=tenant, meta=meta).sync(account.id)
    await db_session.flush()

    rows = {
        row.name: row
        for row in await WhatsAppTemplateRepository(
            db_session, tenant_id=tenant.id
        ).list_for_account(account.id)
    }
    assert rows["scored"].quality_rating == "GREEN"
    assert rows["rated"].quality_rating == "YELLOW"


# ------------------------------------------------------------------ isolation


async def test_one_workspace_cannot_sync_anothers_account(db_session: AsyncSession) -> None:
    mine = await _tenant(db_session, slug="mine-templates")
    theirs = await _tenant(db_session, slug="theirs-templates")
    their_account = await _account(db_session, tenant=theirs)

    with pytest.raises(TenantIsolationError):
        await _service(db_session, tenant=mine, meta=StubMeta(_payload())).sync(their_account.id)


async def test_one_workspace_cannot_read_anothers_templates(db_session: AsyncSession) -> None:
    mine = await _tenant(db_session, slug="reader-mine")
    theirs = await _tenant(db_session, slug="reader-theirs")
    their_account = await _account(db_session, tenant=theirs)

    await _service(db_session, tenant=theirs, meta=StubMeta(_payload())).sync(their_account.id)
    await db_session.flush()

    visible = await _service(db_session, tenant=mine).list_templates()
    assert visible == []


# ------------------------------------------------------- what a follow-up sees


async def _conversation(session: AsyncSession, *, tenant: Tenant) -> Conversation:
    account = await _account(session, tenant=tenant, suffix="conv")
    contact = Contact(tenant_id=tenant.id, wa_id="201000000009")
    session.add(contact)
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
        mode=ConversationMode.AI,
        last_inbound_at=datetime.now(UTC),
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def test_a_follow_up_cannot_be_scheduled_on_a_paused_template(
    db_session: AsyncSession,
) -> None:
    """Refused where a person is present to fix it, not hours later."""
    tenant = await _tenant(db_session, slug="follow-up-paused")
    conversation = await _conversation(db_session, tenant=tenant)
    account = await _account(db_session, tenant=tenant, suffix="tpl")
    await _service(
        db_session,
        tenant=tenant,
        meta=StubMeta(_payload("nudge", status="PAUSED")),
    ).sync(account.id)
    await db_session.flush()

    service = FollowUpService(session=db_session, tenant_id=tenant.id)

    with pytest.raises(ValidationError) as raised:
        await service.schedule(
            conversation_id=conversation.id,
            delay=timedelta(minutes=30),
            template_name="nudge",
            template_language="ar_EG",
        )

    assert "paused" in str(raised.value)


async def test_a_follow_up_on_an_approved_template_is_scheduled(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="follow-up-approved")
    conversation = await _conversation(db_session, tenant=tenant)
    account = await _account(db_session, tenant=tenant, suffix="tpl")
    await _service(db_session, tenant=tenant, meta=StubMeta(_payload("nudge"))).sync(account.id)
    await db_session.flush()

    follow_up = await FollowUpService(session=db_session, tenant_id=tenant.id).schedule(
        conversation_id=conversation.id,
        delay=timedelta(minutes=30),
        template_name="nudge",
        template_language="ar_EG",
    )

    assert follow_up.template_name == "nudge"


async def test_a_template_nobody_has_synced_is_still_allowed(db_session: AsyncSession) -> None:
    """The asymmetry that keeps an un-synced workspace working as it did."""
    tenant = await _tenant(db_session, slug="follow-up-unsynced")
    conversation = await _conversation(db_session, tenant=tenant)

    follow_up = await FollowUpService(session=db_session, tenant_id=tenant.id).schedule(
        conversation_id=conversation.id,
        delay=timedelta(minutes=30),
        template_name="never_synced",
        template_language="ar_EG",
    )

    assert follow_up.template_name == "never_synced"


class StubMessaging:
    """Only the window question, which is all the refusal path needs."""

    def window_open(self, conversation: Conversation) -> bool:
        return False


async def test_a_template_withdrawn_after_scheduling_is_skipped_not_sent(
    db_session: AsyncSession,
) -> None:
    """Meta pauses a template without warning, and hours pass before the send."""
    tenant = await _tenant(db_session, slug="withdrawn-mid-flight")
    conversation = await _conversation(db_session, tenant=tenant)
    account = await _account(db_session, tenant=tenant, suffix="tpl")

    service = FollowUpService(
        session=db_session,
        tenant_id=tenant.id,
        messaging=StubMessaging(),  # type: ignore[arg-type]
    )
    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=timedelta(minutes=30),
        template_name="nudge",
        template_language="ar_EG",
    )
    await db_session.flush()

    await _service(
        db_session,
        tenant=tenant,
        meta=StubMeta(_payload("nudge", status="PAUSED")),
    ).sync(account.id)
    await db_session.flush()

    outcome = await service.dispatch(follow_up)

    assert outcome.status is FollowUpStatus.SKIPPED
    assert outcome.detail is not None and "paused" in outcome.detail
