"""Repositories: the only place database queries are written.

Every workspace-owned read starts from `TenantScopedRepository`, which applies
the tenant filter in one place. The handful of lookups that legitimately cannot
be scoped live in their own small classes so they stay visible.
"""

from .agent_repository import AgentRepository, AgentToolRepository
from .base import BaseRepository, TenantScopedRepository
from .conversation_repository import (
    ContactRepository,
    ConversationRepository,
    MessageRepository,
)
from .invitation_repository import InvitationRepository, InvitationTokenRepository
from .knowledge_repository import (
    DocumentChunkRepository,
    DocumentRepository,
    KnowledgeBaseRepository,
    ScoredChunk,
)
from .lead_repository import (
    LeadActivityRepository,
    LeadFilters,
    LeadNoteRepository,
    LeadRepository,
    LeadStatistics,
)
from .membership_repository import MembershipRepository, UserMembershipRepository
from .tenant_repository import TenantRepository
from .user_repository import UserRepository
from .whatsapp_repository import (
    WhatsAppAccountDirectory,
    WhatsAppAccountRepository,
    WhatsAppEventRepository,
)

__all__ = [
    "AgentRepository",
    "AgentToolRepository",
    "BaseRepository",
    "ContactRepository",
    "ConversationRepository",
    "DocumentChunkRepository",
    "DocumentRepository",
    "InvitationRepository",
    "InvitationTokenRepository",
    "KnowledgeBaseRepository",
    "LeadActivityRepository",
    "LeadFilters",
    "LeadNoteRepository",
    "LeadRepository",
    "LeadStatistics",
    "MembershipRepository",
    "MessageRepository",
    "ScoredChunk",
    "TenantRepository",
    "TenantScopedRepository",
    "UserMembershipRepository",
    "UserRepository",
    "WhatsAppAccountDirectory",
    "WhatsAppAccountRepository",
    "WhatsAppEventRepository",
]
