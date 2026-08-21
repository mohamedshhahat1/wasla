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
    "EMBEDDING_DIMENSIONS",
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
]
