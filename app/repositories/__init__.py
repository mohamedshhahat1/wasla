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
from .follow_up_repository import DueFollowUpClaim, FollowUpRepository
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
from .usage_repository import (
    PlatformUsageRepository,
    TenantUsageTotal,
    UsageEventRepository,
    UsagePoint,
    UsageTotal,
)
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
    "DueFollowUpClaim",
    "FollowUpRepository",
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
    "PlatformUsageRepository",
    "ScoredChunk",
    "TenantRepository",
    "TenantScopedRepository",
    "TenantUsageTotal",
    "UsageEventRepository",
    "UsagePoint",
    "UsageTotal",
    "UserMembershipRepository",
    "UserRepository",
    "WhatsAppAccountDirectory",
    "WhatsAppAccountRepository",
    "WhatsAppEventRepository",
]
