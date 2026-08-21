"""Version 1 of the API."""

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    auth,
    conversations,
    invitations,
    knowledge,
    webhooks,
    whatsapp,
)

api_router = APIRouter()
api_router.include_router(agents.router)
api_router.include_router(auth.router)
api_router.include_router(conversations.router)
api_router.include_router(invitations.router)
api_router.include_router(knowledge.router)
api_router.include_router(webhooks.router)
api_router.include_router(whatsapp.router)

__all__ = ["api_router"]
