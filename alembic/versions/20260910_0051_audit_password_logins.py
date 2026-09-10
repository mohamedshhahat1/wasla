"""add the password-login actions to the audit vocabulary

Revision ID: 0051
Revises: 0050

Two labels: ``login_succeeded`` and ``login_failed``.

Google logins have been audited since migration 0034 and password logins have
not, which left "who signed in to this account, and when" answerable for
federated sessions and unanswerable for password ones (AUTH-04). The counter
``wasla_auth_security_events_total{event="login"}`` covered it in aggregate,
which is the wrong shape for a question about one account.

``invitation_accepted`` and ``invitation_revoked`` are not here. Both have been
in this type since migration 0018 created it; what was missing was any code
writing them, which is a change to the services rather than to the schema.

Additive, like every enum migration in this history. No column is rewritten and
no existing row is touched, so it is safe to apply to a live database ahead of
the code that emits the labels — and it must be applied first. A native
PostgreSQL enum is closed: writing a label the type does not carry raises
``InvalidTextRepresentation`` and aborts the transaction, which is exactly how
AUTH-01 turned six working endpoints into 500s for a fortnight.
"""

from __future__ import annotations

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

NEW_ACTIONS = (
    "login_succeeded",
    "login_failed",
)


def upgrade() -> None:
    # `ADD VALUE` cannot be used later in the transaction that added it, and
    # Alembic runs a migration inside one. `autocommit_block` is the supported
    # way round that, and `IF NOT EXISTS` makes the step re-runnable after a
    # partial failure - which matters precisely because this part is not
    # covered by the surrounding transaction.
    with op.get_context().autocommit_block():
        for value in NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Nothing to undo, and undoing would be the dangerous direction.

    PostgreSQL cannot drop an enum label. Doing it properly means creating a
    replacement type, rewriting every ``audit_logs`` row onto it, swapping the
    column and dropping the old type - a full rewrite of the one table in this
    schema that only ever grows, to remove two labels that are inert the moment
    nothing emits them. Migrations 0025, 0029, 0034, 0036, 0045, 0046, 0047,
    0049 and 0050 all took this position.

    The asymmetry is deliberate rather than lazy. Downgrading past this
    migration while the application still writes ``login_succeeded`` would turn
    every successful password login into a 500 with a full rollback - the
    AUTH-01 failure exactly - so the safe order for a rollback is to deploy the
    older code first and leave the labels standing.
    """
