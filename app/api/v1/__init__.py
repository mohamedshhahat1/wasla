"""Versioned API router.

Business routers (auth, tenants, users, whatsapp, agents, conversations,
messages, contacts, leads, knowledge, follow-ups, campaigns, analytics, usage,
billing, admin, webhooks) are mounted here as their phases land. The aggregate
router exists from Phase 0 so the mount point and version prefix are fixed.
"""

from fastapi import APIRouter

api_router = APIRouter()

__all__ = ["api_router"]
