"""Grant the application role data access after Alembic has changed the schema.

Run with MIGRATION_DATABASE_URL and DATABASE_URL set to different identities for
the same PostgreSQL database. This runs on every deploy, including existing
volumes where docker-entrypoint-initdb.d would never run.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine


async def provision() -> None:
    migration_url = make_url(os.environ["MIGRATION_DATABASE_URL"])
    runtime_url = make_url(os.environ["DATABASE_URL"])
    if (
        migration_url.drivername != "postgresql+asyncpg"
        or runtime_url.drivername != "postgresql+asyncpg"
        or not runtime_url.username
        or not runtime_url.password
        or migration_url.username == runtime_url.username
        or (migration_url.host, migration_url.port, migration_url.database)
        != (runtime_url.host, runtime_url.port, runtime_url.database)
    ):
        raise ValueError("migration and runtime URLs must use distinct roles on the same database")

    engine = create_async_engine(migration_url, echo=False, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            role = await connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                    "FROM pg_roles WHERE rolname = :name"
                ),
                {"name": runtime_url.username},
            )
            existing = role.one_or_none()
            if existing is not None:
                if any(existing):
                    raise ValueError("existing runtime role has elevated privileges")
                membership = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_auth_members m "
                        "JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = :name)"
                    ),
                    {"name": runtime_url.username},
                )
                if membership:
                    raise ValueError("existing runtime role is a member of another role")

            statement = await connection.scalar(
                text(
                    "SELECT format(CAST(:template AS text), CAST(:name AS text), "
                    "CAST(:password AS text))"
                ),
                {
                    "template": (
                        (
                            "ALTER ROLE %I WITH LOGIN PASSWORD %L "
                            if existing
                            else "CREATE ROLE %I WITH LOGIN PASSWORD %L "
                        )
                        + "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                        + "NOREPLICATION NOBYPASSRLS"
                    ),
                    "name": runtime_url.username,
                    "password": runtime_url.password,
                },
            )
            await connection.exec_driver_sql(statement)

            # These are cluster/schema privileges, intentionally outside Alembic.
            # Reapply after each migration so new tables and sequences are usable.
            for template, params in (
                ("REVOKE CREATE ON DATABASE %I FROM PUBLIC", [runtime_url.database]),
                ("REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC", [runtime_url.database]),
                ("REVOKE CREATE ON SCHEMA public FROM PUBLIC", []),
                (
                    "GRANT CONNECT ON DATABASE %I TO %I",
                    [runtime_url.database, runtime_url.username],
                ),
                ("GRANT USAGE ON SCHEMA public TO %I", [runtime_url.username]),
                (
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I",
                    [runtime_url.username],
                ),
                (
                    "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO %I",
                    [runtime_url.username],
                ),
                (
                    "ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I",
                    [migration_url.username, runtime_url.username],
                ),
                (
                    "ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public "
                    "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I",
                    [migration_url.username, runtime_url.username],
                ),
            ):
                placeholders = ", ".join(
                    f"CAST(:arg{index} AS text)" for index in range(len(params))
                )
                separator = ", " if placeholders else ""
                sql = f"SELECT format(CAST(:template AS text){separator}{placeholders})"
                values = {
                    "template": template,
                    **{f"arg{i}": value for i, value in enumerate(params)},
                }
                command = await connection.scalar(text(sql), values)
                await connection.exec_driver_sql(command)

            await _restrict_evidence(connection, runtime_url.username)
    finally:
        await engine.dispose()


# Evidence the runtime role writes and may never rewrite (DB-006). The blanket
# grant above is re-applied on every deploy, so these are re-applied after it:
# the final state is the same however many times this runs. A table-level
# REVOKE also removes column grants, which is why the column grant comes last.
EVIDENCE_TABLES = ("audit_logs", "billing_incidents")
# The columns resolving an incident writes - and nothing about what happened.
INCIDENT_RESOLUTION_COLUMNS = (
    "status",
    "resolved_at",
    "resolved_by",
    "resolution_note",
    "updated_at",
)


async def _restrict_evidence(connection: AsyncConnection, role: str) -> None:
    """Append-only audit trail; incidents resolvable, never rewritten or deleted."""
    for table in EVIDENCE_TABLES:
        present = await connection.scalar(
            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"}
        )
        if not present:
            continue
        command = await connection.scalar(
            text(
                "SELECT format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM %I',"
                " CAST(:t AS text), CAST(:r AS text))"
            ),
            {"t": table, "r": role},
        )
        await connection.exec_driver_sql(command)
        if table == "billing_incidents":
            command = await connection.scalar(
                text(
                    "SELECT format('GRANT UPDATE (%s) ON %I TO %I', CAST(:columns AS text),"
                    " CAST(:t AS text), CAST(:r AS text))"
                ),
                {"columns": ", ".join(INCIDENT_RESOLUTION_COLUMNS), "t": table, "r": role},
            )
            await connection.exec_driver_sql(command)


if __name__ == "__main__":
    asyncio.run(provision())
