"""Repositories: the only place database queries are written.

Every workspace-owned read starts from `TenantScopedRepository`, which applies
the tenant filter in one place. The handful of lookups that legitimately cannot
be scoped live in their own small classes so they stay visible.
"""

from .base import BaseRepository, TenantScopedRepository
from .invitation_repository import InvitationRepository, InvitationTokenRepository
from .membership_repository import MembershipRepository, UserMembershipRepository
from .tenant_repository import TenantRepository
from .user_repository import UserRepository
from .whatsapp_repository import (
    WhatsAppAccountDirectory,
    WhatsAppAccountRepository,
    WhatsAppEventRepository,
)

__all__ = [
    "BaseRepository",
    "InvitationRepository",
    "InvitationTokenRepository",
    "MembershipRepository",
    "TenantRepository",
    "TenantScopedRepository",
    "UserMembershipRepository",
    "UserRepository",
    "WhatsAppAccountDirectory",
    "WhatsAppAccountRepository",
    "WhatsAppEventRepository",
]
