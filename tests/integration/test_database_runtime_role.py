"""Exercise the production role split against a real PostgreSQL server."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.provision_runtime_db_role import provision

pytestmark = pytest.mark.integration


async def test_runtime_role_can_write_data_but_cannot_manage_schema_or_roles(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    suffix = uuid.uuid4().hex[:12]
    role_name = f"wasla_runtime_test_{suffix}"
    table_name = f"security_role_probe_{suffix}"
    future_table_name = f"security_role_future_{suffix}"
    migration_url = make_url(database_url)
    runtime_url = migration_url.set(username=role_name, password=f"test-{suffix}")
    owner = create_async_engine(migration_url)
    runtime = None
    try:
        async with owner.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE TABLE public.{table_name} (id BIGSERIAL PRIMARY KEY, marker text NOT NULL)"
            )

        monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url.render_as_string(False))
        monkeypatch.setenv("DATABASE_URL", runtime_url.render_as_string(False))
        await provision()
        await provision()  # existing-volume deployments and repeat releases are idempotent
        async with owner.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE TABLE public.{future_table_name} (id BIGSERIAL PRIMARY KEY)"
            )
        runtime = create_async_engine(runtime_url)

        async with runtime.begin() as connection:
            flags = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
            assert flags == (False, False, False, False)
            record_id = await connection.scalar(
                text(
                    f"INSERT INTO public.{table_name} (marker) VALUES ('before') RETURNING id"  # noqa: S608
                )
            )
            assert record_id is not None
            await connection.execute(
                text(
                    f"UPDATE public.{table_name} SET marker = 'after' WHERE id = :id"  # noqa: S608
                ),
                {"id": record_id},
            )
            assert (
                await connection.scalar(
                    text(f"SELECT marker FROM public.{table_name} WHERE id = :id"),  # noqa: S608
                    {"id": record_id},
                )
                == "after"
            )
            await connection.execute(
                text(f"DELETE FROM public.{table_name} WHERE id = :id"),  # noqa: S608
                {"id": record_id},
            )
            await connection.exec_driver_sql(
                f"INSERT INTO public.{future_table_name} DEFAULT VALUES"
            )

        for statement in (
            f"CREATE TABLE public.security_forbidden_{suffix} (id integer)",
            f"DROP TABLE public.{table_name}",
            f"CREATE ROLE forbidden_{suffix}",
        ):
            with pytest.raises(DBAPIError):
                async with runtime.begin() as connection:
                    await connection.exec_driver_sql(statement)
        with pytest.raises(DBAPIError):
            async with runtime.connect() as connection:
                await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.exec_driver_sql(f"CREATE DATABASE forbidden_{suffix}")
    finally:
        if runtime is not None:
            await runtime.dispose()
        async with owner.begin() as connection:
            await connection.exec_driver_sql(f"DROP TABLE IF EXISTS public.{future_table_name}")
            await connection.exec_driver_sql(f"DROP TABLE IF EXISTS public.{table_name}")
            await connection.exec_driver_sql(f"DROP OWNED BY {role_name}")
            await connection.exec_driver_sql(f"DROP ROLE {role_name}")
        await owner.dispose()
