"""Metadata guarantees for the agent tables.

Read from the mapped metadata rather than a database, so they run in the unit
suite and catch drift against migration 0005 without PostgreSQL. The companion
in `tests/integration/test_agent_persistence.py` proves the constraints those
declarations promise are actually enforced.
"""

from __future__ import annotations

from sqlalchemy import Table, UniqueConstraint

from app.db.models.agent import (
    DEFAULT_MEMORY_MESSAGE_LIMIT,
    DEFAULT_MEMORY_TOKEN_BUDGET,
    DEFAULT_TEMPERATURE,
    Agent,
    AgentStatus,
    AgentTool,
)


def _index_names(table: Table) -> set[str]:
    return {index.name for index in table.indexes if index.name is not None}


def _unique_columns(table: Table, name: str) -> tuple[str, ...]:
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and constraint.name == name:
            return tuple(column.name for column in constraint.columns)
    raise AssertionError(f"{table.name} has no unique constraint named {name}")


def test_agent_tables_declare_the_indexes_migration_0005_creates():
    assert _index_names(Agent.__table__) == {
        "ix_agents_tenant_id",
        "ix_agents_tenant_id_status",
    }
    assert _index_names(AgentTool.__table__) == {
        "ix_agent_tools_tenant_id",
        "ix_agent_tools_agent_id",
    }


def test_enum_values_match_the_migration_literals():
    assert [member.value for member in AgentStatus] == ["draft", "active", "disabled"]


def test_agent_names_are_unique_per_workspace_not_globally():
    """Two businesses may both call their assistant "Sales"."""
    assert _unique_columns(Agent.__table__, "uq_agents_tenant_id_name") == (
        "tenant_id",
        "name",
    )


def test_a_tool_is_granted_once_per_agent():
    assert _unique_columns(
        AgentTool.__table__,
        "uq_agent_tools_tenant_id_agent_id_name",
    ) == ("tenant_id", "agent_id", "name")


def test_tenant_foreign_keys_cascade():
    for table in (Agent.__table__, AgentTool.__table__):
        (foreign_key,) = table.c.tenant_id.foreign_keys
        assert foreign_key.column.table.name == "tenants"
        assert foreign_key.ondelete == "CASCADE"


def test_a_grant_dies_with_its_agent():
    (foreign_key,) = AgentTool.__table__.c.agent_id.foreign_keys
    assert foreign_key.column.table.name == "agents"
    assert foreign_key.ondelete == "CASCADE"


def test_enum_defaults_are_application_side():
    """Migration 0005 declares no server default for the status column.

    A server_default here would put the metadata and the migration in
    disagreement, and env.py compares server defaults.
    """
    column = Agent.__table__.c.status
    assert column.server_default is None
    assert column.default is not None


def test_audit_timestamps_have_server_defaults():
    for table in (Agent.__table__, AgentTool.__table__):
        assert table.c.created_at.server_default is not None
        assert table.c.updated_at.server_default is not None


def test_the_agent_row_holds_no_provider_credential():
    """A model id is configuration; an API key is not (ADR-007).

    Matched on whole column names rather than substrings: `max_output_tokens`
    and `memory_token_budget` are budgets, and a substring search for "token"
    would flag both and teach the suite to be ignored.
    """
    forbidden = {
        "api_key",
        "access_token",
        "api_token",
        "credential",
        "openai_api_key",
        "secret",
        "token",
    }
    assert forbidden.isdisjoint(Agent.__table__.columns.keys())


def test_configuration_defaults_are_declared_once():
    """The column default and the repository signature read the same constant."""
    assert Agent.__table__.c.temperature.default.arg == DEFAULT_TEMPERATURE
    assert Agent.__table__.c.memory_message_limit.default.arg == DEFAULT_MEMORY_MESSAGE_LIMIT
    assert Agent.__table__.c.memory_token_budget.default.arg == DEFAULT_MEMORY_TOKEN_BUDGET


def test_a_new_agent_is_a_draft_and_answers_nobody():
    """DRAFT exists so an agent can be reviewed before it faces a customer."""
    assert Agent.__table__.c.status.default.arg is AgentStatus.DRAFT
    assert Agent(status=AgentStatus.DRAFT).is_answering is False


def test_only_an_active_agent_answers():
    assert Agent(status=AgentStatus.ACTIVE).is_answering is True
    assert Agent(status=AgentStatus.DISABLED).is_answering is False


def test_agents_are_retired_rather_than_deleted():
    """Status is the retirement mechanism, so there is no soft-delete column."""
    assert "deleted_at" not in Agent.__table__.columns
