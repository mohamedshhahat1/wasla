"""Meta's WhatsApp rate cards.

Revision ID: 0119
Revises: 0118

COST-01 (ADR-150). Platform-level tables - a rate card is Meta's price list,
the same for every workspace, so none of them carries a `tenant_id`:

1. `whatsapp_rate_cards`: one uploaded card - currency, the date it applies
   from, the file's name and SHA-256, who uploaded it, when it was activated.
2. `whatsapp_rate_card_rates`: a market's exact price for a category
   (`numeric(12, 6)`), cascading with its card.
3. `whatsapp_rate_card_markets`: the card's calling-code -> market map.

The default map is a code constant (`app/services/pricing_markets.py`) copied
onto each card at upload, not seed data here: a later correction of the table
then never reprices a card already uploaded.

`audit_action` gains `rate_card_uploaded` and `rate_card_activated`.

**Online.** New, empty tables; the enum values in an autocommit block.

**Downgrade refuses** while any card exists or the trail records either action.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
LABELS = ("rate_card_uploaded", "rate_card_activated")
CATEGORIES = ("marketing", "utility", "authentication", "authentication_international", "service")
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0113)'"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in LABELS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{label}'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        "whatsapp_rate_cards",
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("source_filename", sa.String(255), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("uploaded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_rate_cards"),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name="fk_whatsapp_rate_cards_uploaded_by_id_users",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "char_length(currency) = 3", name=op.f("ck_whatsapp_rate_cards_currency_code")
        ),
    )
    op.create_index("ix_whatsapp_rate_cards_activated_at", "whatsapp_rate_cards", ["activated_at"])
    op.create_table(
        "whatsapp_rate_card_rates",
        sa.Column("rate_card_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("market", sa.String(80), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("rate", sa.Numeric(12, 6), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_rate_card_rates"),
        sa.ForeignKeyConstraint(
            ["rate_card_id"],
            ["whatsapp_rate_cards.id"],
            name="fk_whatsapp_rate_card_rates_rate_card_id_whatsapp_rate_cards",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "rate_card_id", "market", "category", name="uq_whatsapp_rate_card_rates_card_market"
        ),
        sa.CheckConstraint("rate >= 0", name=op.f("ck_whatsapp_rate_card_rates_rate_non_negative")),
        sa.CheckConstraint(
            "category IN (" + ", ".join(f"'{c}'" for c in CATEGORIES) + ")",
            name=op.f("ck_whatsapp_rate_card_rates_category_known"),
        ),
    )
    op.create_table(
        "whatsapp_rate_card_markets",
        sa.Column("rate_card_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("calling_code", sa.String(6), nullable=False),
        sa.Column("market", sa.String(80), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_rate_card_markets"),
        sa.ForeignKeyConstraint(
            ["rate_card_id"],
            ["whatsapp_rate_cards.id"],
            name="fk_whatsapp_rate_card_markets_rate_card_id_whatsapp_rate_cards",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "rate_card_id", "calling_code", name="uq_whatsapp_rate_card_markets_card_code"
        ),
        sa.CheckConstraint(
            "calling_code ~ '^[0-9]{1,6}$'",
            name=op.f("ck_whatsapp_rate_card_markets_calling_code_digits"),
        ),
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    cards = bind.exec_driver_sql("SELECT count(*) FROM whatsapp_rate_cards").scalar_one()
    if cards:
        found.append(f"rate cards: {cards}")
    labels = ", ".join(f"'{label}'" for label in LABELS)
    audited = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text IN ({labels})"  # noqa: S608 - module constants
    ).scalar_one()
    if audited:
        found.append(f"audit_logs recording a rate card upload or activation: {audited}")
    if found:
        raise RuntimeError(
            f"Rate cards cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_table("whatsapp_rate_card_markets")
    op.drop_table("whatsapp_rate_card_rates")
    op.drop_table("whatsapp_rate_cards")
