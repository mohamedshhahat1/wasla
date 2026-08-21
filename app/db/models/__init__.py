"""Declarative models. Importing this package registers every table on `Base`."""

from app.db.base import Base

from .agent import Agent, AgentStatus, AgentTool
from .conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from .enums import InvitationStatus, PlatformRole, TenantRole, TenantStatus
from .invitation import TenantInvitation
from .knowledge import (
    EMBEDDING_DIMENSIONS,
    Document,
    DocumentChunk,
    DocumentSource,
    DocumentStatus,
    KnowledgeBase,
)
from .lead import (
    AGENT_WRITABLE_FIELDS,
    ALLOWED_TRANSITIONS,
    MAX_SCORE,
    MIN_SCORE,
    TERMINAL_STATUSES,
    ActorKind,
    Lead,
    LeadActivity,
    LeadActivityKind,
    LeadNote,
    LeadSource,
    LeadStatus,
    clamp_score,
)
from .membership import Membership
from .tenant import Tenant
from .user import User
from .whatsapp import (
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)

__all__ = [
    "AGENT_WRITABLE_FIELDS",
    "ALLOWED_TRANSITIONS",
    "EMBEDDING_DIMENSIONS",
    "MAX_SCORE",
    "MIN_SCORE",
    "TERMINAL_STATUSES",
    "ActorKind",
    "Agent",
    "AgentStatus",
    "AgentTool",
    "Base",
    "Contact",
    "Conversation",
    "ConversationMode",
    "ConversationStatus",
    "Document",
    "DocumentChunk",
    "DocumentSource",
    "DocumentStatus",
    "InvitationStatus",
    "KnowledgeBase",
    "Lead",
    "LeadActivity",
    "LeadActivityKind",
    "LeadNote",
    "LeadSource",
    "LeadStatus",
    "Membership",
    "Message",
    "MessageDirection",
    "MessageKind",
    "MessageStatus",
    "PlatformRole",
    "Tenant",
    "TenantInvitation",
    "TenantRole",
    "TenantStatus",
    "User",
    "WhatsAppAccount",
    "WhatsAppAccountStatus",
    "WhatsAppEvent",
    "WhatsAppEventKind",
    "WhatsAppEventState",
    "clamp_score",
]
