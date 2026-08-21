"""Model registry.

``Base.metadata`` and Alembic autogeneration only see models that are imported
here, so every model module must be re-exported. ``alembic/env.py`` imports
``Base`` from this module for exactly that reason.
"""

from app.db.base import Base
from app.db.models.enums import InvitationStatus, PlatformRole, TenantRole, TenantStatus
from app.db.models.invitation import TenantInvitation
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User

__all__ = [
    "Base",
    "InvitationStatus",
    "Membership",
    "PlatformRole",
    "Tenant",
    "TenantInvitation",
    "TenantRole",
    "TenantStatus",
    "User",
]
