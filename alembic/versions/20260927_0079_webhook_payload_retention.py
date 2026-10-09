"""Processed WhatsApp webhook payloads are kept for a bounded window.

Revision ID: 0079
Revises: 0078

DB-011: `whatsapp_events.payload` held the raw webhook of every inbound message
and every delivery status - customer text and phone numbers included - for the
workspace's lifetime, NOT NULL, read by nothing after processing. The retention
worker now clears the payload of a processed event older than
`WHATSAPP_EVENT_PAYLOAD_RETENTION_DAYS` (30 by default) and records when. The
event row itself stays: its event id is what deduplicates Meta's retries, and
its state and timestamps are what recovery reads.

- `payload` becomes nullable (metadata only) and `payload_redacted_at` is added.
- A CHECK allows a missing payload only with `payload_redacted_at` set, so a
  payload cannot vanish any other way.
- `ix_whatsapp_events_redactable` is the sweep's backlog: processed events still
  holding a payload, by `processed_at`. Built `CONCURRENTLY` - this is the
  fastest-growing table - and rebuilt on a retry if a failed build left it
  INVALID.

Downgrade refuses once any payload has been redacted: the NOT NULL it would
restore cannot hold a redacted row, and inventing a payload for it would be
worse.

**Downgrade (MIG-0091 survey, ADR-132)** drops the index in the run's transaction
under a 15 s lock_timeout, no longer `CONCURRENTLY` in an autocommit block: a
lower migration refusing used to leave the stamp at this revision without the index,
and `upgrade head` never rebuilt it. The upgrade is unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None

INDEX = "ix_whatsapp_events_redactable"


def upgrade() -> None:
    op.add_column(
        "whatsapp_events",
        sa.Column("payload_redacted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("whatsapp_events", "payload", nullable=True)
    op.execute(
        "ALTER TABLE whatsapp_events ADD CONSTRAINT ck_whatsapp_events_payload_present_or_redacted"
        " CHECK (payload IS NOT NULL OR payload_redacted_at IS NOT NULL) NOT VALID"
    )
    op.execute(
        "ALTER TABLE whatsapp_events VALIDATE CONSTRAINT"
        " ck_whatsapp_events_payload_present_or_redacted"
    )
    with op.get_context().autocommit_block():
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT count(*) FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid"
                    " WHERE c.relname = :name AND NOT x.indisvalid"
                ),
                {"name": INDEX},
            )
            .scalar_one()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON whatsapp_events (processed_at)"
            " WHERE state = 'processed' AND payload IS NOT NULL"
        )


def downgrade() -> None:
    redacted = (
        op.get_bind()
        .exec_driver_sql("SELECT count(*) FROM whatsapp_events WHERE payload IS NULL")
        .scalar_one()
    )
    if redacted:
        raise RuntimeError(
            f"{redacted} webhook payload(s) have been redacted, and the schema before 0079"
            " requires one on every event. Refusing to downgrade; nothing has been changed."
        )
    # In the run's transaction, not `CONCURRENTLY` (MIG-0091): an autocommit
    # block commits every downgrade step above this one, so a refusal further
    # down would leave the stamp here without this index, and `upgrade head` would
    # never rebuild it. Run with the application stopped (docs/RUNBOOK.md);
    # the drop waits at most 15 s for its lock, then fails the whole run.
    op.execute("SET LOCAL lock_timeout = '15s'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.drop_constraint(
        op.f("ck_whatsapp_events_payload_present_or_redacted"), "whatsapp_events", type_="check"
    )
    op.alter_column("whatsapp_events", "payload", nullable=False)
    op.drop_column("whatsapp_events", "payload_redacted_at")
