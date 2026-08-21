"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import AuthServiceDep, CurrentUserDep
from app.schemas.auth import (
    AccessTokenResponse,
    LoginRequest,
    LogoutRequest,
    ProfileResponse,
    RefreshRequest,
    RegistrationRequest,
    SessionResponse,
    WorkspaceSummary,
    WorkspaceSwitchRequest,
)
from app.services.auth_service import AuthenticatedSession, WorkspaceContext

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _summarise(workspace: WorkspaceContext) -> WorkspaceSummary:
    return WorkspaceSummary(
        id=workspace.tenant.id,
        name=workspace.tenant.name,
        slug=workspace.tenant.slug,
        role=workspace.membership.role,
    )


def _session_response(result: AuthenticatedSession) -> SessionResponse:
    return SessionResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        active_workspace=_summarise(result.workspace) if result.workspace else None,
    )


@router.post(
    "/register",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and its first workspace",
)
async def register(payload: RegistrationRequest, service: AuthServiceDep) -> SessionResponse:
    result = await service.register(
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        workspace_name=payload.workspace_name,
        workspace_slug=payload.workspace_slug,
    )
    return _session_response(result)


@router.post(
    "/login",
    response_model=SessionResponse,
    summary="Exchange credentials for a token pair",
)
async def login(payload: LoginRequest, service: AuthServiceDep) -> SessionResponse:
    result = await service.login(
        email=payload.email,
        password=payload.password,
        workspace_slug=payload.workspace_slug,
    )
    return _session_response(result)


@router.post(
    "/refresh",
    response_model=SessionResponse,
    summary="Rotate a refresh token for a new pair",
)
async def refresh(payload: RefreshRequest, service: AuthServiceDep) -> SessionResponse:
    result = await service.refresh(
        refresh_token=payload.refresh_token,
        workspace_slug=payload.workspace_slug,
    )
    return _session_response(result)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    # Stated rather than inferred. Under postponed annotations FastAPI resolves
    # a `-> None` return to NoneType, which it then treats as a real response
    # model and rejects against a 204.
    response_model=None,
    summary="Revoke a refresh token",
)
async def logout(payload: LogoutRequest, service: AuthServiceDep) -> None:
    await service.logout(refresh_token=payload.refresh_token)


@router.post(
    "/workspace",
    response_model=AccessTokenResponse,
    summary="Switch the active workspace",
)
async def switch_workspace(
    payload: WorkspaceSwitchRequest,
    current_user: CurrentUserDep,
    service: AuthServiceDep,
) -> AccessTokenResponse:
    result = await service.select_workspace(
        user=current_user.user,
        workspace_slug=payload.workspace_slug,
    )
    return AccessTokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        active_workspace=_summarise(result.workspace),
    )


@router.get(
    "/me",
    response_model=ProfileResponse,
    summary="Describe the authenticated caller",
)
async def me(current_user: CurrentUserDep, service: AuthServiceDep) -> ProfileResponse:
    workspaces = await service.list_workspaces(user=current_user.user)
    user = current_user.user
    return ProfileResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        platform_role=user.platform_role,
        workspaces=[_summarise(workspace) for workspace in workspaces],
    )
