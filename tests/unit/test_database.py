"""Database foundation tests.

Creating an engine does not open connections, so these tests stay hermetic.
The failure probe points at a closed local port, which is refused immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.db.base import Base, SoftDeleteMixin
from app.db.session import Database


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


async def test_engine_is_configured_from_settings() -> None:
    database = Database(
        _settings(
            database_url="postgresql+asyncpg://wasla:pw@db:5432/wasla_test",
            database_pool_size=7,
            database_echo=False,
        )
    )

    try:
        url = database.engine.url
        assert url.drivername == "postgresql+asyncpg"
        assert url.database == "wasla_test"
        assert url.username == "wasla"
        assert database.engine.echo is False
    finally:
        await database.dispose()


async def test_rendered_url_hides_the_password() -> None:
    database = Database(_settings(database_url="postgresql+asyncpg://wasla:pw@db:5432/wasla"))

    try:
        rendered = database.engine.url.render_as_string(hide_password=True)
        assert "pw" not in rendered
    finally:
        await database.dispose()


async def test_check_raises_dependency_unavailable_when_unreachable() -> None:
    database = Database(
        _settings(
            database_url="postgresql+asyncpg://wasla:pw@127.0.0.1:1/wasla",
            health_check_timeout_seconds=1.0,
            database_connect_timeout_seconds=1.0,
        )
    )

    try:
        with pytest.raises(DependencyUnavailableError) as exc_info:
            await database.check()
        assert exc_info.value.status_code == 503
        assert exc_info.value.details == {"dependency": "postgresql"}
    finally:
        await database.dispose()


def test_asyncpg_connect_args_are_skipped_for_other_drivers() -> None:
    assert Database._connect_args(_settings(database_url="sqlite+aiosqlite:///:memory:")) == {}

    args = Database._connect_args(
        _settings(database_url="postgresql+asyncpg://wasla:pw@db:5432/wasla")
    )
    assert args["timeout"] == pytest.approx(5.0)
    assert args["server_settings"]["application_name"] == "wasla"


def test_metadata_uses_explicit_naming_convention() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
    assert Base.metadata.naming_convention["fk"] == (
        "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    )


def test_soft_delete_mixin_reports_state() -> None:
    class Example(SoftDeleteMixin):
        def __init__(self, deleted_at: datetime | None = None) -> None:
            self.deleted_at = deleted_at

    assert not Example().is_deleted
    assert Example(deleted_at=datetime.now(UTC)).is_deleted
