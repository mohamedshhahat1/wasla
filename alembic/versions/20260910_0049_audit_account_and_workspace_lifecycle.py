"""add the account and workspace lifecycle actions to the audit vocabulary

Revision ID: 0049
Revises: 0048

Two groups of labels, and the first group is a repair.

**The repair (AUTH-01).** Four members have existed on the Python
``AuditAction`` since ADR-036 and were never added to the PostgreSQL type:
``password_changed``, ``user_disabled``, ``user_enabled`` and
``user_sessions_revoked``. A native enum is closed, so writing a label it does
not carry raises ``InvalidTextRepresentation`` and aborts the transaction — and
each of these is written *after* the state change it describes. Six endpoints
therefore did their work and then destroyed it:

* ``POST /auth/logout-all``
* ``POST /auth/password``
* ``POST /auth/password/set``
* ``POST /platform/users/{id}/disable``
* ``POST /platform/users/{id}/enable``
* ``DELETE /platform/users/{id}``

All six returned 500 with a full rollback, so a password was never changed, a
leaked session was never revoked, and an account an administrator believed they
had disabled stayed live. The revocation lever the whole design rests on did not
work at all.

Migration 0018 created the type with the fourteen labels that existed then, and
every extension since has been a deliberate ``ALTER TYPE`` in its own migration.
These four had none, and nothing noticed because the test suite built its schema
with ``Base.metadata.create_all`` — which derives the enum from the Python
members, so the missing labels were present in every test and absent in every
deployment. ``tests/integration/test_schema_parity.py`` closes that gap, and CI
runs it against a migration-built database.

**The new labels.** Seven for the account and workspace lifecycle this change
introduces: an identity tombstoned, and a workspace created, updated,
transferred, suspended, restored or deleted.

Additive only, like every enum migration in this history: no column is rewritten
and no existing row is touched, so this is safe to apply to a live database
ahead of the code that emits the labels.
"""

from __future__ import annotations

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

# The four that should have arrived with ADR-036 and did not.
REPAIRED_ACTIONS = (
    "password_changed",
    "user_disabled",
    "user_enabled",
    "user_sessions_revoked",
)

# The lifecycle vocabulary this change adds.
NEW_ACTIONS = (
    "user_deleted",
    "workspace_created",
    "workspace_updated",
    "workspace_ownership_transferred",
    "workspace_suspended",
    "workspace_restored",
    "workspace_deleted",
)


def upgrade() -> None:
    # `ADD VALUE` cannot be used later in the transaction that added it, and
    # Alembic runs a migration inside one. `autocommit_block` is the supported
    # way round that, and `IF NOT EXISTS` makes the step re-runnable after a
    # partial failure - which matters precisely because this part is not
    # covered by the surrounding transaction.
    #
    # `IF NOT EXISTS` also makes the repaired four safe on a database that has
    # already been patched by hand during the incident.
    with op.get_context().autocommit_block():
        for value in (*REPAIRED_ACTIONS, *NEW_ACTIONS):
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Nothing to undo, and undoing would be the dangerous direction.

    PostgreSQL cannot drop an enum label. Doing it properly means creating a
    replacement type, rewriting every ``audit_logs`` row onto it, swapping the
    column and dropping the old type — a full rewrite of the one table in this
    schema that only ever grows, to remove eleven labels that are inert the
    moment nothing emits them. Migrations 0025, 0029, 0034, 0036, 0045, 0046 and
    0047 all took this position.

    Here the argument is stronger than in any of those. Removing these four in
    particular is precisely the state that caused AUTH-01, so a downgrade that
    "worked" would recreate a production incident on purpose.
    """
