"""Domain enumerations.

Each enumeration is stored as a native PostgreSQL enum type. The Python member
value is exactly what the database stores, so the API, the ORM and the database
never disagree about spelling.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SqlEnum


class TenantStatus(StrEnum):
    """Lifecycle state of a tenant workspace."""

    ACTIVE = "active"
    SUSPENDED = "suspended"


class PlatformRole(StrEnum):
    """Roles that act across every tenant. Reserved for platform staff.

    Customers never hold a platform role; their permissions live on a
    membership and are therefore always scoped to one tenant.
    """

    PLATFORM_OWNER = "platform_owner"
    PLATFORM_ADMIN = "platform_admin"


class TenantRole(StrEnum):
    """Roles held inside one tenant, through a membership."""

    TENANT_OWNER = "tenant_owner"
    TENANT_ADMIN = "tenant_admin"
    MEMBER = "member"


class MembershipStatus(StrEnum):
    """Whether a membership still grants anything.

    Two states, not three. "Suspended" was considered and dropped: it would
    behave identically to revoked at every decision point in the product, and a
    status whose only difference is the word used to describe it invites a call
    site to treat one of them as harmless.
    """

    ACTIVE = "active"
    REVOKED = "revoked"


class InvitationStatus(StrEnum):
    """Lifecycle state of a tenant invitation."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


def _enum_type(enum_class: type[StrEnum], *, name: str) -> SqlEnum:
    """Build a native PostgreSQL enum that stores member values, not names.

    Without ``values_callable`` SQLAlchemy stores the member name, which would
    put ``TENANT_OWNER`` in the database while the application works with
    ``tenant_owner``. Migrations declare the same value lists.
    """
    return SqlEnum(
        enum_class,
        name=name,
        native_enum=True,
        values_callable=lambda members: [str(member.value) for member in members],
    )


# One shared type object per enum, reused by every column that needs it, so the
# PostgreSQL type is created exactly once.
TENANT_STATUS_TYPE = _enum_type(TenantStatus, name="tenant_status")
PLATFORM_ROLE_TYPE = _enum_type(PlatformRole, name="platform_role")
TENANT_ROLE_TYPE = _enum_type(TenantRole, name="tenant_role")
INVITATION_STATUS_TYPE = _enum_type(InvitationStatus, name="invitation_status")
MEMBERSHIP_STATUS_TYPE = _enum_type(MembershipStatus, name="membership_status")
