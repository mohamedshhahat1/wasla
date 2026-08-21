"""Schema-shape tests for the tenancy models.

These assert the mapped schema rather than database behaviour, so they need no
PostgreSQL. Runtime isolation is covered by the repository tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import UniqueConstraint

from app.db.models import (
    Base,
    InvitationStatus,
    Membership,
    PlatformRole,
    Tenant,
    TenantInvitation,
    TenantRole,
    TenantStatus,
    User,
)

TENANT_SCOPED_TABLES = ("memberships", "tenant_invitations")


def test_expected_tables_are_registered():
    expected = {"tenants", "users", "memberships", "tenant_invitations"}

    assert expected <= set(Base.metadata.tables)


def test_identity_is_global():
    # Tenancy belongs on the membership, never on the user row.
    assert "tenant_id" not in User.__table__.columns


def test_membership_is_unique_per_user_and_tenant():
    unique_column_sets = {
        tuple(sorted(column.name for column in constraint.columns))
        for constraint in Membership.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("tenant_id", "user_id") in unique_column_sets


@pytest.mark.parametrize("table_name", TENANT_SCOPED_TABLES)
def test_tenant_scoped_tables_index_tenant_id(table_name):
    table = Base.metadata.tables[table_name]
    indexed_columns = {column.name for index in table.indexes for column in index.columns}

    assert "tenant_id" in table.columns
    assert "tenant_id" in indexed_columns


@pytest.mark.parametrize("table_name", TENANT_SCOPED_TABLES)
def test_tenant_rows_are_removed_with_their_tenant(table_name):
    table = Base.metadata.tables[table_name]
    tenant_fk = next(
        foreign_key
        for foreign_key in table.foreign_keys
        if foreign_key.column.table.name == "tenants"
    )

    assert tenant_fk.ondelete == "CASCADE"


def test_invitations_store_only_a_token_hash():
    columns = set(TenantInvitation.__table__.columns.keys())

    assert "token_hash" in columns
    assert "token" not in columns


def test_stored_enum_values_are_stable():
    # These strings live in PostgreSQL enum types: renaming one is a migration,
    # not an edit.
    assert [role.value for role in TenantRole] == ["tenant_owner", "tenant_admin", "member"]
    assert [role.value for role in PlatformRole] == ["platform_owner", "platform_admin"]
    assert InvitationStatus.PENDING.value == "pending"
    assert TenantStatus.ACTIVE.value == "active"


def test_tenant_is_usable_only_while_live():
    tenant = Tenant(name="Acme", slug="acme", status=TenantStatus.ACTIVE)
    assert tenant.is_active

    tenant.status = TenantStatus.SUSPENDED
    assert not tenant.is_active

    tenant.status = TenantStatus.ACTIVE
    tenant.deleted_at = datetime(2026, 8, 21, tzinfo=UTC)
    assert not tenant.is_active
    assert tenant.is_deleted


def test_platform_role_is_opt_in():
    assert not User(email="member@example.com").is_platform_staff
    staff = User(email="staff@example.com", platform_role=PlatformRole.PLATFORM_OWNER)
    assert staff.is_platform_staff


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (TenantRole.TENANT_OWNER, True),
        (TenantRole.TENANT_ADMIN, True),
        (TenantRole.MEMBER, False),
    ],
)
def test_only_owners_and_admins_administer_a_tenant(role, expected):
    assert Membership(role=role).can_administer_tenant is expected


def test_invitation_is_open_until_revoked_or_expired():
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    invitation = TenantInvitation(
        email="new@example.com",
        role=TenantRole.MEMBER,
        status=InvitationStatus.PENDING,
        token_hash="a" * 64,
        expires_at=now + timedelta(days=1),
    )
    assert invitation.is_open(now=now)

    invitation.status = InvitationStatus.REVOKED
    assert not invitation.is_open(now=now)

    invitation.status = InvitationStatus.PENDING
    invitation.expires_at = now - timedelta(seconds=1)
    assert not invitation.is_open(now=now)
