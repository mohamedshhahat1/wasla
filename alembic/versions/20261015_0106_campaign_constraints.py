"""The campaign send rate bounded in the database; a campaign's number is its own workspace's.

Revision ID: 0106
Revises: 0105

CMP-22 (ADR-143).

1. `ck_campaigns_messages_per_minute_bounded` - `messages_per_minute BETWEEN 1
   AND 600`, the bound the request schema and the service already enforce, so
   a writer that bypasses both cannot store a rate the worker divides by.
2. `fk_campaigns_tenant_account` - `(tenant_id, account_id)` references
   `whatsapp_accounts (tenant_id, id)` (`uq_whatsapp_accounts_tenant_id_id`
   already exists), ADR-100's pattern: a campaign cannot name another
   workspace's number even through a writer that skipped the tenant-scoped
   repository. The template's twin needs a `(tenant_id, id)` key on
   `whatsapp_templates`, which does not exist; recorded as an accepted
   residual rather than built here.

**Online.** Both are added `NOT VALID` and then validated: the add takes a
brief lock under a bounded `lock_timeout`, and the validating scan holds only
`SHARE UPDATE EXCLUSIVE` on `campaigns` (and `ROW SHARE` on the parent).

**Downgrade** drops exactly what the upgrade added. Nothing is lost.
"""

from __future__ import annotations

from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
RATE_CHECK = "ck_campaigns_messages_per_minute_bounded"
ACCOUNT_FK = "fk_campaigns_tenant_account"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {RATE_CHECK}"
        " CHECK (messages_per_minute BETWEEN 1 AND 600) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {ACCOUNT_FK}"
        " FOREIGN KEY (tenant_id, account_id) REFERENCES whatsapp_accounts (tenant_id, id)"
        " ON DELETE CASCADE NOT VALID"
    )
    op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {RATE_CHECK}")
    op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {ACCOUNT_FK}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {ACCOUNT_FK}")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {RATE_CHECK}")
