"""The account's language, its last-used workspace, and an invitation's language.

Revision ID: 0141
Revises: 0140

FE-7 (ADR-201), FEB-09 and FEB-36. Three nullable columns and nothing else:

- `users.locale` - the language the person reads Wasla and its e-mail in, one
  of en / ar / fr / tr, or NULL for "not chosen" (every e-mail in English, as
  before this revision).
- `users.last_workspace_id` - the workspace the person last chose, which a
  sign-in naming none opens while it is still active and the membership still
  stands. `ON DELETE SET NULL`: the product tombstones workspaces and never
  deletes the row, but a pointer must not be able to dangle.
- `tenant_invitations.locale` - the language the inviter chose for the e-mail,
  because the invitee may have no account to read one from.

No backfill: every existing row keeps today's behaviour.

**Online.** Nullable columns without a default are catalog changes; the CHECK
constraints hold trivially over NULL and are validated against the existing
rows by a scan that takes no write lock beyond the `ALTER`'s.

**Downgrade** drops the three columns and their constraints. What is lost is a
preference and a pointer: e-mails go back to English, and a sign-in naming no
workspace goes back to the oldest active one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
LOCALE_CHECK = "locale IS NULL OR locale IN ('en', 'ar', 'fr', 'tr')"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("users", sa.Column("locale", sa.String(length=8), nullable=True))
    op.create_check_constraint(op.f("ck_users_locale_supported"), "users", LOCALE_CHECK)
    op.add_column(
        "users",
        sa.Column("last_workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_users_last_workspace_id_tenants"),
        "users",
        "tenants",
        ["last_workspace_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("tenant_invitations", sa.Column("locale", sa.String(length=8), nullable=True))
    op.create_check_constraint(
        op.f("ck_tenant_invitations_locale_supported"), "tenant_invitations", LOCALE_CHECK
    )


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_constraint(
        op.f("ck_tenant_invitations_locale_supported"), "tenant_invitations", type_="check"
    )
    op.drop_column("tenant_invitations", "locale")
    op.drop_constraint(op.f("fk_users_last_workspace_id_tenants"), "users", type_="foreignkey")
    op.drop_column("users", "last_workspace_id")
    op.drop_constraint(op.f("ck_users_locale_supported"), "users", type_="check")
    op.drop_column("users", "locale")
