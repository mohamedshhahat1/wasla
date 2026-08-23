"""Metadata guarantees for the WhatsApp tables.

These tests read the mapped metadata rather than a database, so they run in the
unit suite and catch drift against migration 0003 without PostgreSQL. They exist
because the tenant index really did go missing from both tables: declaring
``__table_args__`` in a class body replaces the one ``TenantScopedMixin``
contributes, and nothing complains until ``alembic check`` compares the metadata
with the migrated schema.
"""

from __future__ import annotations

from sqlalchemy import Table, UniqueConstraint

from app.db.models import (
    Base,
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)

# Column names that would mean a *plaintext* credential had been persisted.
# ADR-009 forbade any credential column at all; ADR-034 supersedes it with one
# that may hold ciphertext only, so the rule is now about what a column could be
# mistaken for rather than about its existence.
PLAINTEXT_HINTS = ("token", "secret", "credential", "password")
# The one column allowed to look like a credential, because its name says what
# it actually contains.
ENCRYPTED_COLUMN = "access_token_encrypted"


def _index_names(table: Table) -> set[str]:
    return {index.name for index in table.indexes if index.name is not None}


def _unique_columns(table: Table, name: str) -> tuple[str, ...]:
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and constraint.name == name:
            return tuple(column.name for column in constraint.columns)
    raise AssertionError(f"{table.name} has no unique constraint named {name}")


def _leads_with_tenant(table: Table) -> bool:
    """Whether any index on this table starts with `tenant_id`.

    Leading is the property that matters. PostgreSQL uses a composite index for
    a predicate on its first column, so `(tenant_id, occurred_at)` serves a
    tenant-scoped read exactly as a bare `(tenant_id)` would - and adding the
    bare one alongside it would cost every write for nothing.
    """
    return any(
        index.columns is not None and list(index.columns)[:1] == [table.columns["tenant_id"]]
        for index in table.indexes
    )


def test_every_table_with_a_tenant_column_indexes_it():
    """The regression guard for the missing WhatsApp tenant indexes.

    Written against the whole metadata rather than the two tables that were
    broken, so a tenant-scoped model added in a later phase cannot reintroduce
    the same drift.

    The rule is "an index leading with tenant_id", not "an index named
    ix_<table>_tenant_id". The defect being guarded against was a table with no
    way to find one workspace's rows; a table whose first index column is the
    tenant does not have that defect, whatever the index is called.
    """
    missing = sorted(
        table.name
        for table in Base.metadata.tables.values()
        if "tenant_id" in table.columns and not _leads_with_tenant(table)
    )
    assert missing == []


def test_whatsapp_tables_declare_the_indexes_migration_0003_creates():
    assert _index_names(WhatsAppAccount.__table__) == {"ix_whatsapp_accounts_tenant_id"}
    assert _index_names(WhatsAppEvent.__table__) == {
        "ix_whatsapp_events_tenant_id",
        "ix_whatsapp_events_account_id",
        "ix_whatsapp_events_tenant_id_state",
    }


def test_enum_values_match_the_migration_literals():
    assert [member.value for member in WhatsAppAccountStatus] == ["active", "disabled"]
    assert [member.value for member in WhatsAppEventKind] == [
        "message",
        "status",
        "unsupported",
    ]
    assert [member.value for member in WhatsAppEventState] == [
        "received",
        "processed",
        "failed",
    ]


def test_phone_number_id_is_unique_platform_wide():
    """Not (tenant_id, phone_number_id): tenant resolution depends on this."""
    columns = _unique_columns(
        WhatsAppAccount.__table__,
        "uq_whatsapp_accounts_phone_number_id",
    )
    assert columns == ("phone_number_id",)


def test_event_idempotency_is_scoped_to_one_workspace():
    columns = _unique_columns(
        WhatsAppEvent.__table__,
        "uq_whatsapp_events_tenant_id_event_id",
    )
    assert columns == ("tenant_id", "event_id")


def test_the_account_row_stores_no_plaintext_credential():
    """ADR-009 refused a credential column until encryption existed; ADR-034
    adds one that holds ciphertext only.

    The guard survives the change rather than being deleted with it: any column
    that looks like a credential must be the encrypted one, so a plaintext
    `access_token` cannot be added later without this failing.
    """
    suspicious = [
        column.name
        for column in WhatsAppAccount.__table__.columns
        if any(hint in column.name.lower() for hint in PLAINTEXT_HINTS)
    ]
    assert suspicious == [ENCRYPTED_COLUMN]


def test_the_encrypted_credential_is_nullable():
    """A workspace without its own token sends through the platform credential,
    which is how every workspace worked before the column existed."""
    assert WhatsAppAccount.__table__.c[ENCRYPTED_COLUMN].nullable is True


def test_tenant_foreign_keys_cascade():
    for table in (WhatsAppAccount.__table__, WhatsAppEvent.__table__):
        (foreign_key,) = table.c.tenant_id.foreign_keys
        assert foreign_key.column.table.name == "tenants"
        assert foreign_key.ondelete == "CASCADE"


def test_enum_defaults_are_application_side():
    """Migration 0003 declares no server default for the enum columns.

    A server_default here would put the metadata and the migration in
    disagreement, and env.py compares server defaults.
    """
    for table, column_name in (
        (WhatsAppAccount.__table__, "status"),
        (WhatsAppEvent.__table__, "state"),
    ):
        column = table.c[column_name]
        assert column.server_default is None
        assert column.default is not None


def test_audit_timestamps_have_server_defaults():
    for table in (WhatsAppAccount.__table__, WhatsAppEvent.__table__):
        assert table.c.created_at.server_default is not None
        assert table.c.updated_at.server_default is not None


def test_is_active_reflects_status():
    account = WhatsAppAccount(status=WhatsAppAccountStatus.ACTIVE)
    assert account.is_active is True

    account.status = WhatsAppAccountStatus.DISABLED
    assert account.is_active is False
