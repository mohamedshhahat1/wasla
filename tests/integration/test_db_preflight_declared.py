"""`db_preflight verify` sees what the code declares and the database lacks (PO-02).

Before MIG-0091 was fixed, a downgrade crossing 0091 that a lower migration
refused left the database stamped 0091 without
`ix_conversations_tenant_id_channel_last_message_at`; `alembic upgrade head`
never rebuilt it, and `verify` printed ok - it only looked at objects that
exist. These tests run the operator's own command, `python -m
scripts.db_preflight verify`, as a subprocess against databases `alembic
upgrade head` built, and read its exit code and output.

Each test gets a copy of one migration-built template, so a test that damages
its database damages nobody else's.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from scripts.db_preflight import MIGRATION_ONLY_INDEXES, declared_schema
from tests.integration.test_entitlement_migrations import HEAD, _admin, _alembic, _one

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
INDEX = "ix_conversations_tenant_id_channel_last_message_at"


def _present(url: str, name: str) -> bool:
    return bool(_one(url, "SELECT count(*) FROM pg_indexes WHERE indexname = :n", {"n": name}))


@pytest.fixture(scope="module")
def template(database_url: str) -> Iterator[str]:
    """A database at head, built once by the migrations, copied by each test."""
    name = f"wasla_preflight_tpl_{uuid.uuid4().hex[:10]}"
    source = make_url(database_url)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    try:
        _alembic(source.set(database=name).render_as_string(hide_password=False), "upgrade", "head")
        yield name
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


@pytest.fixture
def copy(database_url: str, template: str) -> Iterator[str]:
    name = f"wasla_preflight_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name} TEMPLATE {template}"))
    try:
        yield source.set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


def _verify(url: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "MIGRATION_DATABASE_URL": url}
    environment.pop("DATABASE_URL", None)
    return subprocess.run(  # noqa: S603 - the repository's own module
        [sys.executable, "-m", "scripts.db_preflight", "verify"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_preflight_is_clean_on_a_fresh_head(copy: str) -> None:
    result = _verify(copy)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "db_preflight verify: ok\n",
        "",
    )
    # Non-vacuous: the comparison really covers the index MIG-0091 lost.
    assert ("conversations", INDEX) in declared_schema().indexes
    # Nothing is excused without a reason, and nothing by pattern.
    assert all(reason.strip() for reason in MIGRATION_ONLY_INDEXES.values())
    assert not any("*" in name or "%" in name for name in MIGRATION_ONLY_INDEXES)


def test_preflight_reports_an_index_the_model_declares_but_the_database_lacks(copy: str) -> None:
    asyncio.run(_admin(copy, f"DROP INDEX {INDEX}"))
    assert not _present(copy, INDEX)

    result = _verify(copy)

    assert result.returncode == 1
    assert result.stderr == f"db_preflight verify: index missing: conversations.{INDEX}\n"
    assert result.stdout == ""


def test_preflight_reports_an_invalid_index(copy: str) -> None:
    asyncio.run(
        _admin(
            copy,
            f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = '{INDEX}'::regclass",  # noqa: S608
        )
    )
    assert _present(copy, INDEX)

    result = _verify(copy)

    assert result.returncode == 1
    assert result.stderr == f"db_preflight verify: index invalid: conversations.{INDEX}\n"


def test_preflight_reports_a_missing_constraint_by_table_and_name(copy: str) -> None:
    asyncio.run(
        _admin(
            copy,
            "ALTER TABLE topup_purchases DROP CONSTRAINT ck_topup_purchases_grant_is_not_a_sale",
        )
    )

    result = _verify(copy)

    assert result.returncode == 1
    assert result.stderr == (
        "db_preflight verify: check constraint missing: "
        "topup_purchases.ck_topup_purchases_grant_is_not_a_sale\n"
    )


def test_preflight_detects_the_pre_fix_damage(copy: str) -> None:
    """The state the unfixed 0091 left, rebuilt by hand, then `upgrade head`.

    The old downgrade committed everything down to 0091 and dropped the index
    outside the run; the refusal below then rolled back only 0091's own
    stamp update. So: at 0091, no index, stamped 0091 - and `upgrade head`
    from there never builds it.
    """
    _alembic(copy, "downgrade", "0091")
    asyncio.run(_admin(copy, f"DROP INDEX {INDEX}"))
    assert _one(copy, "SELECT version_num FROM alembic_version") == "0091"
    _alembic(copy, "upgrade", "head")
    assert _one(copy, "SELECT version_num FROM alembic_version") == HEAD
    assert not _present(copy, INDEX)

    result = _verify(copy)

    assert result.returncode == 1
    assert f"index missing: conversations.{INDEX}" in result.stderr
