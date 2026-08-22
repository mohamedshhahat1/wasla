"""Version 1 of the API."""

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    analytics,
    auth,
    campaigns,
    contacts,
    conversations,
    follow_ups,
    invitations,
    knowledge,
    leads,
    platform,
    templates,
    usage,
    webhooks,
    whatsapp,
)

api_router = APIRouter()
api_router.include_router(agents.router)
api_router.include_router(analytics.router)
api_router.include_router(auth.router)
api_router.include_router(campaigns.router)
api_router.include_router(contacts.router)
api_router.include_router(conversations.router)
api_router.include_router(follow_ups.router)
api_router.include_router(invitations.router)
api_router.include_router(knowledge.router)
api_router.include_router(leads.router)
api_router.include_router(platform.router)
api_router.include_router(templates.router)
api_router.include_router(usage.router)
api_router.include_router(webhooks.router)
api_router.include_router(whatsapp.router)

__all__ = ["api_router"]
