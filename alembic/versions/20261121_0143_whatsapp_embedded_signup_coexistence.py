"""WhatsApp Embedded Signup and Coexistence (ADR-204).

Revision ID: 0143
Revises: 0142

MG9-01 .. MG9-06. One migration, and nothing beside what the stage needs:

- ``whatsapp_accounts`` gains how the number was connected
  (``onboarding_method``, ``assisted`` for every existing row), which WhatsApp
  it is on (``onboarding_mode``), the Messaging account id, the sealed
  registration PIN, the Embedded Signup idempotency key, each onboarding step's
  time, the two one-time syncs' requests and retries, the history import's
  progress, and Meta's own words when it disconnected the number.
- ``messages`` gains ``imported`` (history from the WhatsApp Business app) and
  the edit / revoke markers.
- ``connection_disabled_reason`` and ``connection_health`` gain
  ``provider_disconnected``; ``whatsapp_event_kind`` gains ``history``,
  ``state_sync``, ``revision`` and ``account_update``. Appended in an
  autocommit block, as every label is.

**Online.** Every new column is nullable or has a constant default, so
PostgreSQL adds it without rewriting the table; the indexes are partial and
small. ``ALTER TYPE ... ADD VALUE`` takes no table lock.

**Downgrade** drops exactly the columns and indexes the upgrade added. The
enum labels stay - PostgreSQL cannot drop one - and the downgrade refuses
while a row uses one of them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None

LABELS: tuple[tuple[str, str], ...] = (
    ("connection_disabled_reason", "provider_disconnected"),
    ("connection_health", "provider_disconnected"),
    ("whatsapp_event_kind", "history"),
    ("whatsapp_event_kind", "state_sync"),
    ("whatsapp_event_kind", "revision"),
    ("whatsapp_event_kind", "account_update"),
)

STAMP = sa.DateTime(timezone=True)
LOCK_TIMEOUT = "15s"

ACCOUNT_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("onboarding_method", sa.String(20), nullable=False, server_default="assisted"),
    sa.Column("onboarding_mode", sa.String(20), nullable=True),
    sa.Column("messaging_account_id", sa.String(64), nullable=True),
    sa.Column("registration_pin_encrypted", sa.Text(), nullable=True),
    sa.Column("onboarding_idempotency_key", sa.String(255), nullable=True),
    sa.Column("onboarded_at", STAMP, nullable=True),
    sa.Column("subscribed_at", STAMP, nullable=True),
    sa.Column("registered_at", STAMP, nullable=True),
    sa.Column("sync_deadline_at", STAMP, nullable=True),
    sa.Column("sync_next_attempt_at", STAMP, nullable=True),
    sa.Column("contacts_sync_requested_at", STAMP, nullable=True),
    sa.Column("contacts_sync_request_id", sa.String(255), nullable=True),
    sa.Column("contacts_sync_attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("history_sync_requested_at", STAMP, nullable=True),
    sa.Column("history_sync_request_id", sa.String(255), nullable=True),
    sa.Column("history_sync_attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("history_progress", sa.Integer(), nullable=True),
    sa.Column("history_phase_max", sa.Integer(), nullable=True),
    sa.Column("history_completed_at", STAMP, nullable=True),
    sa.Column("history_declined_at", STAMP, nullable=True),
    sa.Column("provider_disconnect_reason", sa.String(64), nullable=True),
    sa.Column("provider_disconnect_initiated_by", sa.String(16), nullable=True),
    sa.Column("provider_disconnected_at", STAMP, nullable=True),
)

MESSAGE_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("imported", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    sa.Column("edited_at", STAMP, nullable=True),
    sa.Column("edited_body", sa.Text(), nullable=True),
    sa.Column("revoked_at", STAMP, nullable=True),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for type_name, label in LABELS:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{label}'")

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    for column in ACCOUNT_COLUMNS:
        op.add_column("whatsapp_accounts", column.copy())
    op.create_check_constraint(
        op.f("ck_whatsapp_accounts_onboarding_method_known"),
        "whatsapp_accounts",
        "onboarding_method IN ('assisted', 'embedded_signup')",
    )
    op.create_check_constraint(
        op.f("ck_whatsapp_accounts_onboarding_mode_known"),
        "whatsapp_accounts",
        "onboarding_mode IS NULL OR onboarding_mode IN ('cloud_api', 'coexistence')",
    )
    op.create_index(
        "uq_whatsapp_accounts_onboarding_idempotency",
        "whatsapp_accounts",
        ["tenant_id", "onboarding_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("onboarding_idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_whatsapp_accounts_onboarded_at",
        "whatsapp_accounts",
        ["onboarded_at"],
        postgresql_where=sa.text("onboarding_method = 'embedded_signup'"),
    )
    op.create_index(
        "ix_whatsapp_accounts_sync_due",
        "whatsapp_accounts",
        ["sync_next_attempt_at"],
        postgresql_where=sa.text(
            "onboarding_mode = 'coexistence' AND released_at IS NULL "
            "AND (contacts_sync_request_id IS NULL OR history_sync_request_id IS NULL)"
        ),
    )
    op.create_index("ix_whatsapp_accounts_waba_id", "whatsapp_accounts", ["waba_id"])

    for column in MESSAGE_COLUMNS:
        op.add_column("messages", column.copy())


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    bind = op.get_bind()
    checks = (
        ("channel_connections", "disabled_reason::text = 'provider_disconnected'"),
        ("channel_connections", "health::text = 'provider_disconnected'"),
        (
            "whatsapp_events",
            "kind::text IN ('history', 'state_sync', 'revision', 'account_update')",
        ),
    )
    for table, condition in checks:
        used = bind.exec_driver_sql(
            f"SELECT count(*) FROM {table} WHERE {condition}"  # noqa: S608
        ).scalar_one()
        if used:
            raise RuntimeError(
                f"Refusing to downgrade 0143: {used} rows of {table} use a label 0143 added."
            )

    for column in reversed(MESSAGE_COLUMNS):
        op.drop_column("messages", column.name)

    op.drop_index("ix_whatsapp_accounts_waba_id", table_name="whatsapp_accounts")
    op.drop_index("ix_whatsapp_accounts_sync_due", table_name="whatsapp_accounts")
    op.drop_index("ix_whatsapp_accounts_onboarded_at", table_name="whatsapp_accounts")
    op.drop_index("uq_whatsapp_accounts_onboarding_idempotency", table_name="whatsapp_accounts")
    op.drop_constraint(
        op.f("ck_whatsapp_accounts_onboarding_mode_known"), "whatsapp_accounts", type_="check"
    )
    op.drop_constraint(
        op.f("ck_whatsapp_accounts_onboarding_method_known"), "whatsapp_accounts", type_="check"
    )
    for column in reversed(ACCOUNT_COLUMNS):
        op.drop_column("whatsapp_accounts", column.name)
