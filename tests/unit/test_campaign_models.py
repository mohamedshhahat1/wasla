"""What the campaign tables promise, before any of them holds a row.

The interesting assertions here are about constraints and indexes rather than
behaviour. A campaign's safety properties are all delegated to the database —
one row per person, a partial index over the pending ones, cascades that do not
erase evidence — so they are checked on the metadata, where a later edit that
quietly drops one of them fails immediately rather than in production.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Index, Table

from app.db.models import Base
from app.db.models.campaign import (
    DEFAULT_MESSAGES_PER_MINUTE,
    MAX_MESSAGES_PER_MINUTE,
    MAX_RECIPIENT_ATTEMPTS,
    TERMINAL_CAMPAIGN_STATUSES,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    OptOutSource,
    RecipientStatus,
)
from app.db.models.conversation import Contact

CAMPAIGNS = Base.metadata.tables["campaigns"]
RECIPIENTS = Base.metadata.tables["campaign_recipients"]
CONTACTS = Base.metadata.tables["contacts"]


def _index(table: Table, name: str) -> Index:
    found = next((index for index in table.indexes if index.name == name), None)
    assert found is not None
    return found


# --------------------------------------------------------------- the schema


def test_a_person_appears_in_a_campaign_once() -> None:
    """The idempotency key: a restarted worker must not send twice."""
    names = {constraint.name for constraint in RECIPIENTS.constraints}

    assert "uq_campaign_recipients_campaign_id_contact_id" in names


def test_the_pending_index_covers_only_pending_recipients() -> None:
    """Finished work is the bulk of the table and must not be scanned."""
    index = _index(RECIPIENTS, "ix_campaign_recipients_pending")

    assert index is not None
    assert index.dialect_options["postgresql"]["where"] is not None


def test_the_due_index_covers_only_campaigns_that_could_send() -> None:
    index = _index(CAMPAIGNS, "ix_campaigns_due")

    assert index is not None
    assert index.dialect_options["postgresql"]["where"] is not None


def test_both_campaign_tables_are_tenant_scoped() -> None:
    assert "tenant_id" in CAMPAIGNS.c
    assert "tenant_id" in RECIPIENTS.c
    assert _index(CAMPAIGNS, "ix_campaigns_tenant_id") is not None
    assert _index(RECIPIENTS, "ix_campaign_recipients_tenant_id") is not None


def test_deleting_a_message_does_not_erase_the_record_that_it_was_sent() -> None:
    """SET NULL, not CASCADE: the recipient row is the evidence."""
    message_fk = next(fk for fk in RECIPIENTS.c.message_id.foreign_keys)
    conversation_fk = next(fk for fk in RECIPIENTS.c.conversation_id.foreign_keys)

    assert message_fk.ondelete == "SET NULL"
    assert conversation_fk.ondelete == "SET NULL"


def test_a_recipient_goes_with_its_campaign() -> None:
    campaign_fk = next(fk for fk in RECIPIENTS.c.campaign_id.foreign_keys)

    assert campaign_fk.ondelete == "CASCADE"


def test_a_campaign_must_name_a_template() -> None:
    """There is no free-text body, and never will be: Meta would refuse it."""
    assert "body" not in CAMPAIGNS.c
    assert CAMPAIGNS.c.template_id.nullable is False


def test_a_contact_records_when_it_opted_out_not_merely_that_it_did() -> None:
    assert "marketing_opt_out_at" in CONTACTS.c
    assert CONTACTS.c.marketing_opt_out_at.nullable is True
    assert "opt_out_source" in CONTACTS.c


# -------------------------------------------------------------- the vocabulary


def test_a_paused_campaign_is_not_finished_but_a_cancelled_one_is() -> None:
    """The distinction that gives a hesitating person a way back."""
    assert CampaignStatus.PAUSED not in TERMINAL_CAMPAIGN_STATUSES
    assert CampaignStatus.CANCELLED in TERMINAL_CAMPAIGN_STATUSES
    assert CampaignStatus.COMPLETED in TERMINAL_CAMPAIGN_STATUSES
    assert CampaignStatus.FAILED in TERMINAL_CAMPAIGN_STATUSES


def test_a_draft_is_not_finished() -> None:
    assert CampaignStatus.DRAFT not in TERMINAL_CAMPAIGN_STATUSES
    assert CampaignStatus.SCHEDULED not in TERMINAL_CAMPAIGN_STATUSES
    assert CampaignStatus.RUNNING not in TERMINAL_CAMPAIGN_STATUSES


def test_a_campaign_knows_whether_it_is_finished() -> None:
    assert Campaign(status=CampaignStatus.RUNNING).is_finished is False
    assert Campaign(status=CampaignStatus.RUNNING).is_running is True
    assert Campaign(status=CampaignStatus.CANCELLED).is_finished is True
    assert Campaign(status=CampaignStatus.CANCELLED).is_running is False


def test_the_rate_ceiling_protects_the_number_not_metas_throughput() -> None:
    """Far below what Meta allows, on purpose."""
    assert DEFAULT_MESSAGES_PER_MINUTE < MAX_MESSAGES_PER_MINUTE
    assert MAX_MESSAGES_PER_MINUTE <= 600


def test_a_recipient_gives_up_after_a_few_attempts() -> None:
    """Every attempt is a message that might actually arrive."""
    assert MAX_RECIPIENT_ATTEMPTS <= 3

    recipient = CampaignRecipient(status=RecipientStatus.PENDING, attempts=0)
    assert recipient.is_pending is True
    assert recipient.is_exhausted is False

    recipient.attempts = MAX_RECIPIENT_ATTEMPTS
    assert recipient.is_exhausted is True


def test_a_contact_that_has_not_opted_out_accepts_campaigns() -> None:
    assert Contact(wa_id="201000000001").accepts_campaigns is True


def test_a_contact_that_opted_out_does_not() -> None:
    contact = Contact(
        wa_id="201000000001",
        marketing_opt_out_at=datetime.now(UTC),
        opt_out_source=OptOutSource.CUSTOMER,
    )

    assert contact.accepts_campaigns is False
