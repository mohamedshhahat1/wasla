"""Declarative models. Importing this package registers every table on `Base`."""

from app.db.base import Base

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
    "Base",
    "Contact",
    "Conversation",
    "ConversationMode",
    "ConversationStatus",
    "InvitationStatus",
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
