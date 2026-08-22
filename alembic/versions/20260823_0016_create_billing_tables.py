"""create the plan and subscription tables, and seed the catalogue

Revision ID: 0016
Revises: 0015

Two tables, and one data insert that needs justifying.

The four plans documented in `docs/SAAS.md` are inserted here rather than left
to an operator, because a deployment with an empty catalogue cannot onboard
anybody: there is no plan to put a new workspace on, so registration either
fails or invents an entitlement from nowhere. A migration is the one mechanism
that runs exactly once on every environment, which is what seed data wants.

They are seeded, not owned. Editing a price or a limit afterwards is a change to
the row, not to this file, and nothing here re-asserts the values later - an
upgrade runs once, and re-running it against a catalogue somebody has edited is
not a thing Alembic does.

`plans.id` is generated here with `gen_random_uuid()` rather than in Python, so
the insert is a single statement and needs no round trip. That is the one place
in this schema where the database generates a key, and it is safe because
nothing references these rows until a subscription is created.

The subscription foreign key to `plans` is `RESTRICT`, not `CASCADE`: deleting a
plan out from under a paying workspace would leave it entitled to nothing in the
middle of a period it has already paid for.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

BILLING_INTERVAL = postgresql.ENUM(
    "monthly",
    "yearly",
    name="billing_interval",
    create_type=False,
)
SUBSCRIPTION_STATUS = postgresql.ENUM(
    "trialing",
    "active",
    "past_due",
    "cancelled",
    "expired",
    name="subscription_status",
    create_type=False,
)

# The catalogue from docs/SAAS.md. Enterprise carries no limits at all, which is
# what "custom" means here: an absent key is unlimited.
DEFAULT_PLANS = (
    {
        "code": "starter",
        "name": "Starter",
        "description": "One number, one agent, and enough messages to prove it works.",
        "price": "0.00",
        "trial_days": 14,
        "sort_order": 10,
        "limits": {
            "whatsapp_numbers": 1,
            "agents": 1,
            "team_members": 2,
            "knowledge_documents": 25,
            "period_messages": 1000,
            "period_ai_requests": 100,
            "period_campaign_messages": 0,
        },
    },
    {
        "code": "pro",
        "name": "Pro",
        "description": "Several numbers and agents, for a team that answers all day.",
        "price": "99.00",
        "trial_days": 14,
        "sort_order": 20,
        "limits": {
            "whatsapp_numbers": 3,
            "agents": 5,
            "team_members": 10,
            "knowledge_documents": 500,
            "period_messages": 10000,
            "period_ai_requests": 5000,
            "period_campaign_messages": 5000,
        },
    },
    {
        "code": "business",
        "name": "Business",
        "description": "Room for several departments and their campaigns.",
        "price": "299.00",
        "trial_days": 14,
        "sort_order": 30,
        "limits": {
            "whatsapp_numbers": 10,
            "agents": 20,
            "team_members": 50,
            "knowledge_documents": 5000,
            "period_messages": 50000,
            "period_ai_requests": 25000,
            "period_campaign_messages": 25000,
        },
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "description": "Custom limits and pricing, agreed rather than listed.",
        "price": "0.00",
        "trial_days": 0,
        "sort_order": 40,
        # Deliberately empty: an absent limit is unlimited.
        "limits": {},
        "is_public": False,
    },
)


def upgrade():
    bind = op.get_bind()
    BILLING_INTERVAL.create(bind, checkfirst=False)
    SUBSCRIPTION_STATUS.create(bind, checkfirst=False)

    op.create_table(
        "plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("interval", BILLING_INTERVAL, nullable=False),
        sa.Column("trial_days", sa.Integer(), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plans"),
        sa.UniqueConstraint("code", name="uq_plans_code"),
    )
    op.create_index("ix_plans_is_public", "plans", ["is_public"])

    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", SUBSCRIPTION_STATUS, nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_reference", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_subscriptions_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plans.id"],
            name="fk_subscriptions_plan_id_plans",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subscriptions"),
        sa.UniqueConstraint("tenant_id", name="uq_subscriptions_tenant_id"),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index("ix_subscriptions_plan_id", "subscriptions", ["plan_id"])
    op.create_index(
        "ix_subscriptions_current_period_end",
        "subscriptions",
        ["current_period_end"],
    )

    _seed_plans()


def _seed_plans() -> None:
    """Insert the documented catalogue, monthly, in US dollars."""
    statement = sa.text("""
        INSERT INTO plans (
            id, code, name, description, price, currency, interval,
            trial_days, limits, is_public, is_active, sort_order
        )
        VALUES (
            gen_random_uuid(), :code, :name, :description, :price, 'USD', 'monthly',
            :trial_days, CAST(:limits AS jsonb), :is_public, true, :sort_order
        )
        """)
    bind = op.get_bind()
    for plan in DEFAULT_PLANS:
        bind.execute(
            statement,
            {
                "code": plan["code"],
                "name": plan["name"],
                "description": plan["description"],
                "price": plan["price"],
                "trial_days": plan["trial_days"],
                "limits": json.dumps(plan["limits"]),
                "is_public": plan.get("is_public", True),
                "sort_order": plan["sort_order"],
            },
        )


def downgrade():
    bind = op.get_bind()

    op.drop_index("ix_subscriptions_current_period_end", table_name="subscriptions")
    op.drop_index("ix_subscriptions_plan_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_status", table_name="subscriptions")
    op.drop_index("ix_subscriptions_tenant_id", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_plans_is_public", table_name="plans")
    op.drop_table("plans")

    SUBSCRIPTION_STATUS.drop(bind, checkfirst=False)
    BILLING_INTERVAL.drop(bind, checkfirst=False)
