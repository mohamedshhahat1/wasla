"""Domain enumerations.

Each enumeration is stored as a native PostgreSQL enum type. The Python member
value is exactly what the database stores, so the API, the ORM and the database
never disagree about spelling.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from sqlalchemy import DDL
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql


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


# Label order, for the types whose production order is not their members'
# order (DB-015). `ALTER TYPE ... ADD VALUE` appends, so a type that grew over
# many migrations holds its labels in the order they were *added*, while its
# class lists them in the order that reads best. A schema built from the models
# used to take the class's order, so the test schema sorted `ORDER BY status`
# differently from production. These types are created in production's order
# instead - see `ordered_type_ddl` - and the parity tests compare
# `enumsortorder` against this, so the two cannot drift apart again.
DATABASE_LABEL_ORDER: Final[dict[str, tuple[str, ...]]] = {}


def _enum_type(
    enum_class: type[StrEnum],
    *,
    name: str,
    database_order: tuple[str, ...] | None = None,
) -> SqlEnum:
    """Build a native PostgreSQL enum that stores member values, not names.

    Without ``values_callable`` SQLAlchemy stores the member name, which would
    put ``TENANT_OWNER`` in the database while the application works with
    ``tenant_owner``. Migrations declare the same value lists.

    ``database_order`` is the label order production holds, when that is not
    the members' order. The type is then not created with its table's
    metadata; the table creates it in that order with `ordered_type_ddl`.
    """
    values = [str(member.value) for member in enum_class]
    if database_order is None:
        return SqlEnum(
            enum_class,
            name=name,
            native_enum=True,
            values_callable=lambda members: [str(member.value) for member in members],
        )
    if sorted(database_order) != sorted(values):
        raise ValueError(f"{name}: database_order must list every label exactly once")
    DATABASE_LABEL_ORDER[name] = database_order
    return postgresql.ENUM(
        enum_class,
        name=name,
        values_callable=lambda members: [str(member.value) for member in members],
        create_type=False,
    )


def ordered_type_ddl(name: str) -> tuple[DDL, DDL]:
    """CREATE and DROP for a type declared with a ``database_order``.

    Listened to as its table's ``before_create`` and ``after_drop``.
    """
    labels = ", ".join(f"'{label}'" for label in DATABASE_LABEL_ORDER[name])
    return (
        DDL(f"CREATE TYPE {name} AS ENUM ({labels})"),  # type: ignore[no-untyped-call]
        DDL(f"DROP TYPE IF EXISTS {name}"),  # type: ignore[no-untyped-call]
    )


# One shared type object per enum, reused by every column that needs it, so the
# PostgreSQL type is created exactly once.
TENANT_STATUS_TYPE = _enum_type(TenantStatus, name="tenant_status")
PLATFORM_ROLE_TYPE = _enum_type(PlatformRole, name="platform_role")
TENANT_ROLE_TYPE = _enum_type(TenantRole, name="tenant_role")
INVITATION_STATUS_TYPE = _enum_type(InvitationStatus, name="invitation_status")
MEMBERSHIP_STATUS_TYPE = _enum_type(MembershipStatus, name="membership_status")
