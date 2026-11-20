"""Migration 0142: `audit_action` gains `member_role_changed` (MG8-04).

Built from scratch to 0141, upgraded: the label exists, last in the type's
order, as the model's database order declares. Downgraded with no row using
it: the label stays (PostgreSQL cannot drop one) and the chain moves back.
Upgraded again, a row recorded with the label: the downgrade refuses rather
than leave a row the previous revision does not describe.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.engine import make_url

from app.db.models.audit import AUDIT_ACTION_DATABASE_ORDER
from tests.integration.test_fe7_migration import _admin, _alembic, _run

pytestmark = pytest.mark.integration

LABEL = "member_role_changed"


def _labels(url: str) -> list[str]:
    rows = asyncio.run(
        _run(
            url,
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'audit_action' ORDER BY e.enumsortorder",
        )
    )
    return [str(row[0]) for row in rows]


def _version(url: str) -> str:
    return str(asyncio.run(_run(url, "SELECT version_num FROM alembic_version"))[0][0])


def test_0142_adds_the_role_change_label_and_guards_its_downgrade(database_url: str) -> None:
    name = f"wasla_mg8_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    try:
        _alembic(target, "upgrade", "0141")
        assert LABEL not in _labels(target)

        _alembic(target, "upgrade", "0142")
        labels = _labels(target)
        assert labels[-1] == LABEL
        assert tuple(labels) == AUDIT_ACTION_DATABASE_ORDER

        _alembic(target, "downgrade", "0141")
        assert _version(target) == "0141"
        assert LABEL in _labels(target)

        _alembic(target, "upgrade", "0142")
        asyncio.run(
            _run(
                target,
                "INSERT INTO audit_logs (id, action, actor_kind, occurred_at) "
                "VALUES (:id, 'member_role_changed', 'user', now())",
                id=uuid.uuid4(),
            )
        )
        with pytest.raises(RuntimeError, match="Refusing to downgrade 0142"):
            _alembic(target, "downgrade", "0141")
        assert _version(target) == "0142"
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
