"""Give every plan a storage ceiling, so object storage is not unbounded.

Revision ID: 0043
Revises: 0042

`LimitKey.STORAGE_BYTES` is new, and `plans.limits` is JSONB where **an absent
key means unlimited** (ADR-052). So shipping the key without this migration
would ship a limit that is enforced nowhere: every existing plan, and every
plan a deployment has edited, would answer "no ceiling" and the finding would
be closed in code and open in production.

## What this writes

One number, onto every plan that does not already carry the key. Nothing else
changes: no column, no index, no constraint.

## Why one number rather than a tier per plan

Storage is not a product decision anybody has taken here. Inventing one -
5 GB on Starter, 100 GB on Business - would be this migration deciding pricing,
and a limit a customer hits is a limit somebody has to have agreed to sell
them. What *is* a decision the platform can take on its own is a technical
safety ceiling: an authenticated workspace must not be able to create unbounded
storage cost, and 50 GB is far above what any real workspace accumulates while
being a number the platform can absorb.

Where it comes from: an attachment is capped at 25 MB (`MEDIA_MAX_BYTES`), so
this is about two thousand maximum-size files, or a great many ordinary
photographs. A workspace that reaches it has either been running for years or
is doing something the platform wants to know about, and both are conversations
rather than outages.

Tiering it later is an `UPDATE` on four rows. Nothing in the application reads
this number from anywhere but the plan.

## Enterprise

Left alone, and that is the rule the whole encoding rests on: `enterprise`
carries no limits at all because "custom" means "agreed rather than listed",
and writing a ceiling onto it here would silently cap a customer whose contract
says otherwise. A deployment that wants one writes it onto that plan.

## The downgrade

Removes the key rather than restoring a previous value, because there was none.
That returns every plan to unlimited storage, which is exactly the state before
this revision - a downgrade that left a ceiling behind would be a downgrade
that changed behaviour.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

# 50 GiB. A technical safety ceiling rather than a commercial tier - see above.
STORAGE_BYTES = 50 * 1024 * 1024 * 1024

# The plans this applies to. `enterprise` is deliberately absent.
TIERED_PLANS = ("starter", "pro", "business")

# `jsonb_exists` rather than the `?` operator, which a driver using `$1`
# placeholders has to be told is not one. Same test, no ambiguity.
#
# The casts are not decoration: asyncpg prepares every statement and asks
# the server to infer each parameter's type, and inside `jsonb_build_object`
# and `ANY` there is nothing to infer one from. Without them the migration
# fails with `could not determine data type of parameter $1`.
_SET = sa.text("""
    UPDATE plans
       SET limits = limits || jsonb_build_object('storage_bytes', CAST(:bytes AS bigint)),
           updated_at = now()
     WHERE code = ANY(CAST(:codes AS text[]))
       AND NOT jsonb_exists(limits, 'storage_bytes')
    """)

_UNSET = sa.text("""
    UPDATE plans
       SET limits = limits - 'storage_bytes',
           updated_at = now()
     WHERE code = ANY(CAST(:codes AS text[]))
    """)


def upgrade():
    op.get_bind().execute(_SET, {"bytes": STORAGE_BYTES, "codes": list(TIERED_PLANS)})


def downgrade():
    op.get_bind().execute(_UNSET, {"codes": list(TIERED_PLANS)})
