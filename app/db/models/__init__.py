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
from .follow_up import (
    MAX_ATTEMPTS,
    TERMINAL_FOLLOW_UP_STATUSES,
    FollowUp,
    FollowUpStatus,
)
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
from .media import UNRESOLVED_MEDIA_STATUSES, MediaStatus, MessageMedia
from .membership import Membership
from .sentiment import (
    PRIORITY_RANK,
    SENTIMENT_PRIORITY,
    SENTIMENT_SEVERITY,
    ConversationPriority,
    MessageSentiment,
    SentimentLabel,
    is_at_least,
    raised_priority,
)
from .tenant import Tenant
from .user import User
from .whatsapp import (
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)
from .whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
    count_placeholders,
)

__all__ = [
    "AGENT_WRITABLE_FIELDS",
    "ALLOWED_TRANSITIONS",
    "EMBEDDING_DIMENSIONS",
    "MAX_ATTEMPTS",
    "MAX_SCORE",
    "MIN_SCORE",
    "PRIORITY_RANK",
    "SENTIMENT_PRIORITY",
    "SENTIMENT_SEVERITY",
    "TERMINAL_FOLLOW_UP_STATUSES",
    "TERMINAL_STATUSES",
    "UNRESOLVED_MEDIA_STATUSES",
    "ActorKind",
    "Agent",
    "AgentStatus",
    "AgentTool",
    "Base",
    "Contact",
    "Conversation",
    "ConversationMode",
    "ConversationPriority",
    "ConversationStatus",
    "Document",
    "DocumentChunk",
    "DocumentSource",
    "DocumentStatus",
    "FollowUp",
    "FollowUpStatus",
    "InvitationStatus",
    "KnowledgeBase",
    "Lead",
    "LeadActivity",
    "LeadActivityKind",
    "LeadNote",
    "LeadSource",
    "LeadStatus",
    "MediaStatus",
    "Membership",
    "Message",
    "MessageDirection",
    "MessageKind",
    "MessageMedia",
    "MessageSentiment",
    "MessageStatus",
    "PlatformRole",
    "SentimentLabel",
    "TemplateCategory",
    "TemplateStatus",
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
    "WhatsAppTemplate",
    "clamp_score",
    "count_placeholders",
    "is_at_least",
    "raised_priority",
]
