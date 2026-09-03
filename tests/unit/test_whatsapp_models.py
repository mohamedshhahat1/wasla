"""Metadata guarantees for the WhatsApp tables.

These tests read the mapped metadata rather than a database, so they run in the
unit suite and catch drift against migration 0003 without PostgreSQL. They exist
because the tenant index really did go missing from both tables: declaring
``__table_args__`` in a class body replaces the one ``TenantScopedMixin``
contributes, and nothing complains until ``alembic check`` compares the metadata
with the migrated schema.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Index, Table, UniqueConstraint

from app.db.models import (
    Base,
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)
from tests.fakes import as_table

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


def _index(table: Table, name: str) -> Index:
    for index in table.indexes:
        if index.name == name:
            return index
    raise AssertionError(f"{table.name} has no index named {name}")


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


def test_every_table_with_a_tenant_column_indexes_it() -> None:
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


def test_whatsapp_tables_declare_the_indexes_the_migrations_create() -> None:
    assert _index_names(as_table(WhatsAppAccount.__table__)) == {
        "ix_whatsapp_accounts_tenant_id",
        # Migration 0022. Unique, and partial: one *live* claim per number.
        "uq_whatsapp_accounts_live_phone_number_id",
    }
    assert _index_names(as_table(WhatsAppEvent.__table__)) == {
        "ix_whatsapp_events_tenant_id",
        "ix_whatsapp_events_account_id",
        "ix_whatsapp_events_tenant_id_state",
    }


def test_enum_values_match_the_migration_literals() -> None:
    assert [member.value for member in WhatsAppAccountStatus] == [
        "active",
        "disabled",
        # Migration 0022. Order matters: PostgreSQL appends new enum labels, so
        # a value inserted in the middle here would not match the type on a
        # database that has been upgraded rather than built fresh.
        "released",
    ]
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


def test_one_live_claim_per_number_platform_wide() -> None:
    """Not (tenant_id, phone_number_id): tenant resolution depends on this.

    A partial unique index rather than a constraint, so that releasing a number
    frees it without deleting the row - and with it every conversation and
    message that cascades from the account (ADR-037).
    """
    index = _index(as_table(WhatsAppAccount.__table__), "uq_whatsapp_accounts_live_phone_number_id")

    assert index.unique is True
    assert [column.name for column in index.columns] == ["phone_number_id"]
    # The predicate is what makes it partial. Without it a released row would
    # still occupy the number.
    predicate = index.dialect_options["postgresql"]["where"]
    assert "released_at IS NULL" in str(predicate)


def test_a_released_row_no_longer_counts_as_active() -> None:
    """`is_active` gates sending and inbound resolution, so it has to see the
    release rather than only the status."""
    account = WhatsAppAccount(
        phone_number_id="1",
        waba_id="2",
        display_phone_number="+20 100 000 0000",
        status=WhatsAppAccountStatus.ACTIVE,
    )
    assert account.is_active is True
    assert account.is_released is False

    account.status = WhatsAppAccountStatus.RELEASED
    account.released_at = datetime(2026, 8, 23, tzinfo=UTC)

    active: bool = account.is_active
    released: bool = account.is_released
    assert active is False
    assert released is True


def test_ownership_proof_is_recorded_on_the_row() -> None:
    """A claim without a recorded proof is a claim an operator cannot audit,
    and the column is what makes the pre-ADR-037 rows findable."""
    columns = as_table(WhatsAppAccount.__table__).columns
    assert "ownership_verified_at" in columns
    # Nullable, because rows claimed before proof existed genuinely have none
    # and back-dating them would erase exactly that list.
    assert columns["ownership_verified_at"].nullable is True


def test_event_idempotency_is_scoped_to_one_workspace() -> None:
    columns = _unique_columns(
        as_table(WhatsAppEvent.__table__),
        "uq_whatsapp_events_tenant_id_event_id",
    )
    assert columns == ("tenant_id", "event_id")


def test_the_account_row_stores_no_plaintext_credential() -> None:
    """ADR-009 refused a credential column until encryption existed; ADR-034
    adds one that holds ciphertext only.

    The guard survives the change rather than being deleted with it: any column
    that looks like a credential must be the encrypted one, so a plaintext
    `access_token` cannot be added later without this failing.
    """
    suspicious = [
        column.name
        for column in as_table(WhatsAppAccount.__table__).columns
        if any(hint in column.name.lower() for hint in PLAINTEXT_HINTS)
    ]
    assert suspicious == [ENCRYPTED_COLUMN]


def test_the_encrypted_credential_is_nullable() -> None:
    """A workspace without its own token sends through the platform credential,
    which is how every workspace worked before the column existed."""
    assert as_table(WhatsAppAccount.__table__).c[ENCRYPTED_COLUMN].nullable is True


def test_tenant_foreign_keys_cascade() -> None:
    for table in (WhatsAppAccount.__table__, WhatsAppEvent.__table__):
        (foreign_key,) = table.c.tenant_id.foreign_keys
        assert foreign_key.column.table.name == "tenants"
        assert foreign_key.ondelete == "CASCADE"


def test_enum_defaults_are_application_side() -> None:
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


def test_audit_timestamps_have_server_defaults() -> None:
    for table in (WhatsAppAccount.__table__, WhatsAppEvent.__table__):
        assert table.c.created_at.server_default is not None
        assert table.c.updated_at.server_default is not None


def test_is_active_reflects_status() -> None:
    account = WhatsAppAccount(status=WhatsAppAccountStatus.ACTIVE)
    assert account.is_active is True

    account.status = WhatsAppAccountStatus.DISABLED
    assert account.is_active is False
