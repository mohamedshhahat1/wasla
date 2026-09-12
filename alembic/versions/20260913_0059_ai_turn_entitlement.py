"""sell AI customer turns, not provider requests

Revision ID: 0059
Revises: 0058

A plan's AI allowance was written as `period_ai_requests` and enforced against
`usage_events` rows of type `ai_request` - one per provider call. A customer turn
makes a sentiment classification and then one to three inference rounds, so the
allowance was being spent by an implementation detail: with exactly one unit left
the classifier took it, the inference was refused, and the customer was never
answered (AI-02). Every ordinary turn cost two units, so a plan advertising 100
bought about fifty answered conversations.

**The allowance is now a count of turns.** `ai_turn` is a new meter, reserved once
per turn by the agent worker in the same transaction that engages the turn, and
`period_ai_turns` is the limit key that counts it. `ai_request` is still recorded
for every provider call, because that is what the platform pays for - it is cost
accounting and is no longer checked against anything.

**Existing plans keep their number, deliberately.** Each plan's
`period_ai_requests` value moves to `period_ai_turns` unchanged. The plan copy
(`docs/SAAS.md`) always described that figure as a customer-facing count, and a
turn costs at least one provider request, so no customer's effective allowance
goes down and no price changes. A plan with no key stays unlimited.

The enum label is added in an autocommit block: `ADD VALUE` cannot be used in the
transaction that added it, and `IF NOT EXISTS` makes a partial run re-runnable.
"""

from __future__ import annotations

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE usage_event_type ADD VALUE IF NOT EXISTS 'ai_turn' AFTER 'ai_request'"
        )

    op.execute("""
        UPDATE plans
           SET limits = (limits - 'period_ai_requests')
                        || jsonb_build_object('period_ai_turns', limits -> 'period_ai_requests')
         WHERE limits ? 'period_ai_requests'
        """)


def downgrade() -> None:
    """Move the key back. The `ai_turn` label stays.

    PostgreSQL cannot drop an enum label, and recreating the type to remove one
    would have to rewrite every usage row; an unused label is harmless.
    """
    op.execute("""
        UPDATE plans
           SET limits = (limits - 'period_ai_turns')
                        || jsonb_build_object('period_ai_requests', limits -> 'period_ai_turns')
         WHERE limits ? 'period_ai_turns'
        """)
