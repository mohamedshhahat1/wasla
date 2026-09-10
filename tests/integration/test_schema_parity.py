"""The migrated database and the models must describe the same schema.

This suite exists because of AUTH-01, and it is worth stating the failure
precisely, because the shape of it is what the assertions below are for.

Four ``AuditAction`` members - ``password_changed``, ``user_disabled``,
``user_enabled``, ``user_sessions_revoked`` - existed in Python and in no
migration. PostgreSQL enums are closed: writing a label the type does not carry
raises ``InvalidTextRepresentation``, which aborts the transaction. Every one of
those four is written by an account-security endpoint *after* the state change
it describes, so six endpoints - sign out everywhere, change password, set a
first password, platform disable, enable and delete - each did their work, tried
to record it, and rolled the whole request back with a 500. In production, with
a fully green test suite.

Green because the suite built its schema with ``Base.metadata.create_all``,
which creates each enum type from the Python members. Under that build the four
labels are present by construction, so the tests could not fail: the schema
under test was derived from the same source as the code under test, and the
migrations - the only thing a deployment actually runs - were never consulted.

``alembic check`` does not close this. It compares tables, columns, indexes and
constraints; it does not compare native enum *labels*, so a migration that
forgets ``ALTER TYPE ... ADD VALUE`` passes it. That was true throughout the
period AUTH-01 was live, and it is asserted below rather than believed.

So the test reads the labels out of ``pg_enum`` on a database built by
``alembic upgrade head`` and compares them to the ``sa.Enum`` definitions the
application maps. Any difference in either direction fails:

* **missing from the database** - a model member no migration added, which is
  AUTH-01 exactly, and breaks every write that reaches for it;
* **extra in the database** - a label nothing maps any more, which is either a
  migration that ran ahead of the code or a member deleted without a plan for
  the rows still carrying it.

The whole file skips unless the schema was built from migrations, because
against a model-built schema it can only ever tautologically pass.
"""

from __future__ import annotations

import collections

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models import Base
from app.db.models.audit import AuditAction
from tests.integration.conftest import built_from_migrations

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not built_from_migrations(),
        reason=(
            "schema parity is only meaningful against a migration-built database; "
            "set WASLA_TEST_SCHEMA=migrations"
        ),
    ),
]

# Every label that has to reach the database for an account-security or
# workspace-lifecycle endpoint to commit. Named individually rather than left to
# the whole-enum comparison below so that a regression reads as "logout-all
# cannot record itself" rather than as a set difference.
LIFECYCLE_ACTIONS = (
    AuditAction.PASSWORD_CHANGED,
    AuditAction.USER_DISABLED,
    AuditAction.USER_ENABLED,
    AuditAction.USER_SESSIONS_REVOKED,
    AuditAction.USER_DELETED,
    AuditAction.WORKSPACE_CREATED,
    AuditAction.WORKSPACE_UPDATED,
    AuditAction.WORKSPACE_OWNERSHIP_TRANSFERRED,
    AuditAction.WORKSPACE_SUSPENDED,
    AuditAction.WORKSPACE_RESTORED,
    AuditAction.WORKSPACE_DELETED,
)


def _model_enums() -> dict[str, set[str]]:
    """Every native enum the mapped tables use, by type name.

    Read off the columns rather than off a list of enum classes, because a
    column is what actually writes a label. A ``StrEnum`` nothing maps cannot
    break a request, and one mapped under two names would be missed by a list.
    """
    found: dict[str, set[str]] = collections.defaultdict(set)
    for table in Base.metadata.tables.values():
        for column in table.columns:
            column_type = column.type
            if isinstance(column_type, sa.Enum) and column_type.name:
                found[column_type.name] |= set(column_type.enums)
    return dict(found)


async def _database_enums(connection: AsyncConnection) -> dict[str, set[str]]:
    """Every enum type in the database, by name, straight from the catalogue."""
    rows = await connection.execute(sa.text("""
            SELECT t.typname, e.enumlabel
            FROM pg_enum e
            JOIN pg_type t ON t.oid = e.enumtypid
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = current_schema()
            """))
    found: dict[str, set[str]] = collections.defaultdict(set)
    for type_name, label in rows:
        found[type_name].add(label)
    return dict(found)


async def test_every_model_enum_matches_the_migrated_database(
    db_connection: AsyncConnection,
) -> None:
    """The whole-schema guard. One assertion, every enum type.

    A new enum member added to a model without its ``ALTER TYPE ... ADD VALUE``
    fails here, on the commit that introduces it, rather than in production on
    the first request that writes it.
    """
    model = _model_enums()
    database = await _database_enums(db_connection)

    differences: dict[str, dict[str, list[str]]] = {}
    for name in sorted(set(model) | set(database)):
        expected = model.get(name, set())
        actual = database.get(name, set())
        if expected != actual:
            differences[name] = {
                "missing_from_database": sorted(expected - actual),
                "absent_from_models": sorted(actual - expected),
            }

    assert not differences, (
        "model and database enums disagree; add an Alembic migration for each "
        f"missing label: {differences}"
    )


async def test_the_audit_actions_the_lifecycle_endpoints_write_all_exist(
    db_connection: AsyncConnection,
) -> None:
    """AUTH-01's regression test, named after what it protects.

    Each of these is written after a state change has already been made. A
    label the type does not carry does not merely lose the audit entry - it
    aborts the transaction and takes the state change with it, which is how six
    working endpoints returned 500 while the suite stayed green.
    """
    labels = (await _database_enums(db_connection)).get("audit_action", set())
    missing = sorted(action.value for action in LIFECYCLE_ACTIONS if action.value not in labels)
    assert not missing, f"audit_action is missing labels the lifecycle endpoints write: {missing}"


async def test_alembic_check_does_not_notice_a_missing_enum_label() -> None:
    """Why this file exists rather than a line in the CI workflow.

    ``alembic check`` is already run by CI and passed throughout the period
    AUTH-01 was live. It compares tables, columns, indexes and constraints;
    SQLAlchemy's autogenerate comparators do not diff native enum labels at all.

    Asserted rather than trusted, because the day it *does* start comparing
    them, this file's argument for existing gets weaker and somebody should
    read it again. A failure here is a prompt to re-read, not a defect.
    """
    from alembic.autogenerate import compare

    comparators = getattr(compare, "comparators", None)
    assert comparators is not None, "alembic.autogenerate.compare lost its dispatcher"
    registered = repr(comparators)
    assert "enum" not in registered.lower(), (
        "alembic autogenerate now appears to register an enum comparator; "
        "re-read whether `alembic check` closes the AUTH-01 class of drift"
    )
