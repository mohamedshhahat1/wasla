"""Versioned API router.

Business routers are mounted here as their phases land. The aggregate router
exists from Phase 0 so the mount point and version prefix are fixed.
"""

from fastapi import APIRouter

from app.api.v1 import auth, invitations

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(invitations.router)

__all__ = ["api_router"]
