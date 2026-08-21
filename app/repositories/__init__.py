"""Repositories: the only place database queries are written.

Keeping queries here is what makes tenant isolation enforceable. See
``base.py`` for the scoping rules every tenant-owned model inherits.
"""

from app.repositories.base import BaseRepository, TenantScopedRepository
from app.repositories.membership_repository import MembershipRepository, UserMembershipRepository
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository

__all__ = [
    "BaseRepository",
    "MembershipRepository",
    "TenantRepository",
    "TenantScopedRepository",
    "UserMembershipRepository",
    "UserRepository",
]
