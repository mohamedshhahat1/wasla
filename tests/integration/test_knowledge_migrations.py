"""Migrations 0127 .. 0134 refuse a downgrade that would lose data (section 28).

Built from scratch to head on a database of its own, written to through the
real `KnowledgeService`, then walked down: each migration in turn refuses while
the data only it can hold exists, says what it would discard, and goes once
that data is gone. A downgrade with nothing to lose then reaches 0126 and the
upgrade back to head succeeds. The populated round trip (rows and columns
preserved across head -> 0126 -> head) is the rehearsal in the Part 1 report.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.tenant import Tenant
from app.services.knowledge_service import KnowledgeService
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _alembic(url: str, action: str, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["wasla_database_url"] = url
    getattr(command, action)(config, revision)


async def _admin(admin_url: str, statement: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


async def _execute(url: str, statement: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _seed(url: str) -> None:
    """One document carrying what each of 0127 .. 0131 added."""
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            tenant = Tenant(
                timezone=TEST_TIMEZONE, name="Down", slug=f"down-{uuid.uuid4().hex[:8]}"
            )
            session.add(tenant)
            await session.flush()
            knowledge = KnowledgeService(session=session, tenant_id=tenant.id, queue=None)
            base = await knowledge.ensure_default_knowledge_base()
            view, _ = await knowledge.submit(
                knowledge_base_id=base.id,
                title="Price list",
                raw="Price list\n\nSuper lux finishing costs 7900 EGP per metre.",
                filename="pricing.txt",
                valid_until=datetime.now(UTC) + timedelta(days=30),
            )
            # A replacement still in flight: its generation carries the new text.
            await knowledge.replace_content(
                view.document.id, raw="Price list. Super lux finishing costs 8100 EGP."
            )
            await knowledge.update_knowledge_base(base.id, name="Handbook")
            await session.commit()
    finally:
        await engine.dispose()
    await _execute(
        url, "UPDATE documents SET extraction_warnings = '{\"digits_glued_to_arabic\": 1}'"
    )


def _refuses(url: str, migration: str) -> None:
    with pytest.raises(RuntimeError, match=f"Refusing to downgrade {migration}"):
        _alembic(url, "downgrade", "0126")


def test_each_new_migration_refuses_to_discard_what_only_it_holds(database_url: str) -> None:
    name = f"wasla_rg1_down_{uuid.uuid4().hex[:10]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    try:
        _alembic(target, "upgrade", "head")
        asyncio.run(_seed(target))

        _refuses(target, "0131")  # the rename's and the replacement's audit rows
        asyncio.run(
            _execute(
                target,
                "DELETE FROM audit_logs WHERE action::text IN ('knowledge_document_replaced',"
                " 'knowledge_document_metadata_updated', 'knowledge_base_updated',"
                " 'knowledge_base_deleted')",
            )
        )
        _refuses(target, "0130")  # valid_until
        asyncio.run(_execute(target, "UPDATE documents SET valid_until = NULL"))
        _refuses(target, "0129")  # the source key the filename derived
        asyncio.run(_execute(target, "UPDATE documents SET source_key = NULL"))
        _refuses(target, "0128")  # the replacement's content snapshot
        asyncio.run(_execute(target, "UPDATE document_index_generations SET content = NULL"))
        _refuses(target, "0127")  # extraction warnings
        asyncio.run(_execute(target, "UPDATE documents SET extraction_warnings = NULL"))

        _alembic(target, "downgrade", "0126")
        _alembic(target, "upgrade", "head")
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


async def _seed_part_two(url: str) -> None:
    """A published chunk with lexical text, a workspace's settings and their audit row."""
    from app.core.config import Settings
    from app.services.knowledge_settings_service import KnowledgeSettingsService
    from tests.fake_embeddings import FakeEmbeddings
    from tests.fakes import as_embeddings

    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            tenant = Tenant(timezone=TEST_TIMEZONE, name="Down", slug=f"dn2-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            await session.flush()
            knowledge = KnowledgeService(session=session, tenant_id=tenant.id, queue=None)
            base = await knowledge.ensure_default_knowledge_base()
            view, _ = await knowledge.submit(
                knowledge_base_id=base.id, title="Prices", raw="BK-35 costs 7900 EGP per metre."
            )
            await knowledge.ingest(
                document_id=view.document.id, embeddings=as_embeddings(FakeEmbeddings())
            )
            await KnowledgeSettingsService(
                session=session,
                tenant_id=tenant.id,
                settings=Settings(_env_file=None, environment="test"),
            ).update({"number_grounding": "strict"}, actor=None)
            await session.commit()
    finally:
        await engine.dispose()


def test_part_two_migrations_refuse_to_discard_what_only_they_hold(database_url: str) -> None:
    """0134 (workspace settings and their audit rows), then 0132 (chunks' lexical text)."""
    name = f"wasla_rg2_down_{uuid.uuid4().hex[:10]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    try:
        _alembic(target, "upgrade", "head")
        asyncio.run(_seed_part_two(target))

        def refuses(migration: str) -> None:
            with pytest.raises(RuntimeError, match=f"Refusing to downgrade {migration}"):
                _alembic(target, "downgrade", "0131")

        refuses("0134")
        asyncio.run(_execute(target, "UPDATE tenants SET knowledge_settings = NULL"))
        refuses("0134")  # the audit row of the change remains
        asyncio.run(
            _execute(
                target,
                "DELETE FROM audit_logs WHERE action::text = 'knowledge_settings_updated'",
            )
        )
        refuses("0132")
        asyncio.run(
            _execute(target, "UPDATE document_chunks SET search_text = NULL, search_tsv = NULL")
        )
        _alembic(target, "downgrade", "0131")
        _alembic(target, "upgrade", "head")
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
