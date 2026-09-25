"""Make free plans permanent: no trial, no expiry (BILL-01, BILL-23).

Revision ID: 0070
Revises: 0069

Starter was seeded as a free plan with a fourteen-day trial. A trial of a free
plan grants nothing, and its expiry moved every workspace to the terminal
`expired` state on day fourteen - after which a paid checkout took the money and
granted nothing. Starter is now simply free, for ever.

Data, not schema:

* **Every free plan gets `trial_days = 0`**, and so does every priced plan: a
  priced plan's trial was dead configuration (checkout goes straight to paid)
  and is removed rather than left to mislead.
* **A subscription still trialing a free plan becomes `active`** on the same
  plan and period. Nothing about what it may do changes.
* **A subscription that expired off a free plan's trial becomes `active`
  again**, with a fresh period starting now. `expired` means exactly that
  nobody chose to end it - a customer's own decision is `cancelled`, which is
  deliberately untouched, as is `suspended` and any subscription on a priced
  plan. Nothing is charged: the plan is free.

Downgrade restores the fourteen-day Starter trial on the catalogue only. It
cannot know which subscriptions it revived, and re-expiring them would recreate
the defect this migration exists to remove.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE plans SET trial_days = 0 WHERE trial_days <> 0")

    op.execute("""
        UPDATE subscriptions s
        SET status = 'active', trial_ends_at = NULL, updated_at = now()
        FROM plans p
        WHERE p.id = s.plan_id
          AND p.price = 0
          AND s.status = 'trialing'
        """)

    # A calendar month from now, clamped the way the application clamps it:
    # PostgreSQL's interval arithmetic already maps 31 January to 28/29
    # February, which is the same rule `billing_calendar` applies.
    op.execute("""
        UPDATE subscriptions s
        SET status = 'active',
            ended_at = NULL,
            trial_ends_at = NULL,
            current_period_start = now(),
            current_period_end = CASE p.interval
                WHEN 'yearly' THEN now() + interval '1 year'
                ELSE now() + interval '1 month'
            END,
            updated_at = now()
        FROM plans p
        WHERE p.id = s.plan_id
          AND p.price = 0
          AND s.status = 'expired'
          AND s.cancelled_at IS NULL
        """)


def downgrade() -> None:
    op.execute(sa.text("UPDATE plans SET trial_days = 14 WHERE code = 'starter'"))
