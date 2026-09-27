"""Every trigger function names its schema; conversations leave room for HOT.

Revision ID: 0080
Revises: 0079

**DB-024.** The trigger functions installed before the database audit resolved
table names through the caller's `search_path`. They are `SECURITY INVOKER` and
the runtime role can create nothing, so nothing was exploitable, but a function
that guards money should not depend on a session setting. Each is pinned to
`public, pg_catalog`, the schema every one of them is written against; the
functions added since the audit (0074, 0076, 0077) were created pinned. Bodies
are unchanged.

**DB-020.** Every message writes its conversation row twice: the sequence
trigger's bump of `last_message_sequence` (no index) and the move of the
indexed `last_message_at`. The first can be a HOT update when the page has
room; `fillfactor = 90` gives it room. Measured with 20,000 single-message
transactions over 500 conversations, the bump was HOT 92% of the time at the
default and 98.6% at 90, and the table's indexes were 7% smaller.

Both statements change catalog metadata only: `ALTER FUNCTION` rewrites no data,
and `SET (fillfactor)` takes SHARE UPDATE EXCLUSIVE - writes continue - and
applies to pages as they are next filled.
"""

from __future__ import annotations

from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None

SEARCH_PATH = "public, pg_catalog"

# Every trigger function created before 0074 without a pinned search_path.
FUNCTIONS = (
    "wasla_assign_message_sequence",
    "plan_versions_refuse_update",
    "billing_refuse_foreign_custom_plan",
    "plans_derive_scope",
    "plans_refuse_tenant_change",
    "payments_refuse_automatic_topup",
    "topup_purchases_refuse_snapshot_change",
    "topup_purchases_refuse_foreign_product",
    "custom_plan_offers_refuse_foreign_or_changed",
    "invoices_refuse_offer_mismatch",
)

FILLFACTOR = 90


def upgrade() -> None:
    for name in FUNCTIONS:
        op.execute(f"ALTER FUNCTION {name}() SET search_path = {SEARCH_PATH}")
    op.execute(f"ALTER TABLE conversations SET (fillfactor = {FILLFACTOR})")


def downgrade() -> None:
    op.execute("ALTER TABLE conversations RESET (fillfactor)")
    for name in FUNCTIONS:
        op.execute(f"ALTER FUNCTION {name}() RESET search_path")
