"""enforce one live password reset token per account in the database

Revision ID: 0052
Revises: 0051

``email_verification_challenges`` has carried
``uq_email_verification_challenges_active`` since it was created: a partial
unique index on ``user_id`` where the row is neither consumed nor superseded, so
"at most one live challenge per account" is a fact PostgreSQL keeps.
``password_reset_tokens`` had no equivalent — the same invariant rested entirely
on ``supersede_outstanding`` being called before ``create`` (AUTH-09). Two
sibling tables enforcing one rule at two strengths, and the weaker one is the
one guarding a password.

**"Live" is the two null columns, not expiry.** ``expires_at > now()`` cannot
appear in an index predicate, which has to be immutable, and it does not need
to: an expired token nothing has superseded is still that account's outstanding
token, ``supersede_outstanding`` ends it with the rest when a new one is issued,
and ``is_usable`` is what refuses to spend it. This matches the sibling index
exactly, which is the point.

**The repair comes first.** A deployment could already hold more than one live
token for an account — the invariant has never been enforced — and
``CREATE UNIQUE INDEX`` would simply fail on such a row, leaving the migration
stuck with no explanation. So the step below supersedes the extras, keeping the
newest, which is precisely what the application would have done had the
supersede not been missed. Nothing is deleted: a superseded token is a row that
records that a reset link was issued and then invalidated, and that is worth
keeping. On a database that never drifted this updates nothing.

**Non-concurrent, deliberately.** ``CREATE INDEX CONCURRENTLY`` cannot run
inside a transaction and cannot be rolled back if it fails, and this table is
small and short-lived by construction — a row per reset request, all of them
dead within thirty minutes. The brief write lock is not worth the operational
complexity of the concurrent form.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_password_reset_tokens_active"
TABLE_NAME = "password_reset_tokens"
LIVE = "consumed_at IS NULL AND superseded_at IS NULL"


def upgrade() -> None:
    # Keep the newest live token per account and supersede the rest. `now()`
    # rather than a literal, so the timestamp says when the repair happened -
    # a row claiming to have been superseded at the moment it was issued would
    # be a small lie in a table that exists to be reasoned about.
    op.execute(
        # Written out rather than interpolated from the constants below. They
        # are module constants and not caller input, so nothing is unsafe about
        # the f-string version - but a literal statement is what a reviewer can
        # read against the schema, and it does not have to be argued with a
        # linter suppression.
        """
        UPDATE password_reset_tokens AS stale
        SET superseded_at = now()
        WHERE stale.consumed_at IS NULL
          AND stale.superseded_at IS NULL
          AND EXISTS (
              SELECT 1
              FROM password_reset_tokens AS newer
              WHERE newer.user_id = stale.user_id
                AND newer.consumed_at IS NULL
                AND newer.superseded_at IS NULL
                AND (newer.created_at, newer.id) > (stale.created_at, stale.id)
          )
        """
    )
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(LIVE),
    )


def downgrade() -> None:
    """Drop the index. The tokens it constrained are left exactly as they are.

    Reversible, unlike the enum migrations either side of it, because an index
    is a constraint on future writes rather than a vocabulary rows are written
    in. Nothing is un-superseded: the repair above ended tokens that the
    application had already stopped treating as the account's live one, and
    resurrecting them would hand somebody back a second working reset link.
    """
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
