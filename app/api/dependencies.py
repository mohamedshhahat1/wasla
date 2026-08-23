"""Authentication and authorization dependencies.

The tenant a request acts on is read from the signed access token, never from a
path, query or body parameter. There is therefore no request field a caller
could forge to aim a route at another workspace's data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.dependencies import RedisDep, SessionDep, SettingsDep
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.rate_limit import RateLimiter
from app.core.security import TokenClaims, TokenType, decode_token
from app.core.storage import LocalMediaStorage, MediaStorage
from app.core.token_store import RefreshTokenStore
from app.db.models import Membership, PlatformRole, Tenant, TenantRole, User
from app.db.models.billing import LimitKey
from app.platform.platform_analytics import PlatformAnalyticsService
from app.platform.platform_billing import PlatformBillingService
from app.repositories import MembershipRepository, TenantRepository, UserRepository
from app.repositories.audit_repository import (
    AuditLogRepository,
    PlatformAuditLogRepository,
)
from app.repositories.billing_repository import PlanRepository
from app.services.account_service import AccountService
from app.services.agent_service import AgentService
from app.services.analytics_service import AnalyticsService
from app.services.auth_service import AuthService
from app.services.campaign_service import CampaignService
from app.services.credential_service import CredentialService
from app.services.entitlement_service import Entitlement, EntitlementService
from app.services.follow_up_service import FollowUpService
from app.services.inbox_service import InboxService
from app.services.invitation_service import InvitationService
from app.services.invoice_service import InvoiceService
from app.services.knowledge_service import KnowledgeService
from app.services.lead_service import LeadService
from app.services.media_service import MediaService
from app.services.messaging_service import MessagingService
from app.services.sentiment_service import SentimentService
from app.services.subscription_service import SubscriptionService
from app.services.template_service import TemplateService
from app.services.usage_service import UsageService
from app.services.whatsapp_account_service import WhatsAppAccountService
from app.workers.ingestion_queue import IngestionQueue

# auto_error is off so a missing header raises the same domain error as a bad
# one, and every failure leaves through the same response envelope.
bearer_scheme = HTTPBearer(auto_error=False, description="Access token")

BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def get_auth_service(
    settings: SettingsDep,
    session: SessionDep,
    redis: RedisDep,
) -> AuthService:
    """Built with a limiter as well as a token store.

    The limiter is for the per-account login budget, which the service applies
    rather than the route: the account it counts by is inside the request body,
    and a dependency that read the body to find it would consume the stream
    before the handler saw it.
    """
    return AuthService(
        session=session,
        settings=settings,
        token_store=RefreshTokenStore(redis),
        limiter=RateLimiter(redis),
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_account_service(session: SessionDep) -> AccountService:
    """Account lifecycle and session revocation (ADR-036).

    Not workspace-scoped, and deliberately: an account is a global identity,
    so the authority to suspend one is the platform's rather than any
    workspace's. The routes decide who may call it.
    """
    return AccountService(session)


AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]


def get_invitation_service(session: SessionDep) -> InvitationService:
    return InvitationService(session=session)


InvitationServiceDep = Annotated[InvitationService, Depends(get_invitation_service)]


def get_whatsapp_account_service(
    settings: SettingsDep,
    session: SessionDep,
) -> WhatsAppAccountService:
    """Built with a credential service, so a workspace can supply its own token.

    A deployment with no encryption key configured still connects numbers - it
    simply refuses to store a credential rather than storing one in the clear.
    """
    return WhatsAppAccountService(
        session=session,
        credentials=CredentialService(settings),
    )


WhatsAppAccountServiceDep = Annotated[
    WhatsAppAccountService,
    Depends(get_whatsapp_account_service),
]


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated caller and the token they presented."""

    user: User
    claims: TokenClaims


async def get_current_user(
    settings: SettingsDep,
    session: SessionDep,
    credentials: BearerDep,
) -> CurrentUser:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authentication is required.")

    claims = decode_token(
        credentials.credentials,
        settings=settings,
        expected_type=TokenType.ACCESS,
    )
    user = await UserRepository(session).get_by_id(claims.subject)
    if user is None or not user.is_active:
        # The token is still signed and unexpired, but the account behind it is
        # gone or disabled, so it stops working now rather than at expiry.
        raise AuthenticationError("The credentials are not valid.")
    if claims.token_version != user.token_version:
        # Revocation (ADR-036). The row is already loaded to check `is_active`
        # above, so this comparison costs no extra query - which is the whole
        # reason revocation can be immediate rather than waiting out the access
        # token's fifteen minutes.
        #
        # A token minted before the column existed carries no version at all and
        # lands here too, which is correct: it cannot be shown to be current.
        raise AuthenticationError("The credentials are not valid.")
    return CurrentUser(user=user, claims=claims)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


@dataclass(frozen=True, slots=True)
class ActiveWorkspace:
    """The workspace a request is scoped to, and the caller's standing in it."""

    user: User
    membership: Membership
    tenant: Tenant

    @property
    def role(self) -> TenantRole:
        return self.membership.role


async def get_active_workspace(
    current_user: CurrentUserDep,
    session: SessionDep,
) -> ActiveWorkspace:
    """Resolve the workspace named by the token, re-checking membership.

    The membership is loaded on every request rather than trusted from the
    token, so withdrawing someone's access takes effect at once instead of
    whenever their access token happens to expire.
    """
    tenant_id = current_user.claims.tenant_id
    if tenant_id is None:
        raise PermissionDeniedError("No workspace is selected for this session.")

    memberships = MembershipRepository(session, tenant_id=tenant_id)
    membership = await memberships.require_for_user(current_user.user.id)

    tenant = await TenantRepository(session).get_by_id(tenant_id)
    if tenant is None or not tenant.is_active:
        raise PermissionDeniedError("This workspace is not available.")
    return ActiveWorkspace(
        user=current_user.user,
        membership=membership,
        tenant=tenant,
    )


ActiveWorkspaceDep = Annotated[ActiveWorkspace, Depends(get_active_workspace)]


def get_agent_service(
    settings: SettingsDep,
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> AgentService:
    return AgentService(
        session=session,
        settings=settings,
        tenant_id=workspace.tenant.id,
    )


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]


def get_campaign_service(
    settings: SettingsDep,
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> CampaignService:
    """Workspace-scoped, and deliberately without a messaging service.

    Nothing reachable over the API sends a campaign message. Composing,
    targeting and scheduling are all database work; the sending happens in the
    worker, where ten thousand messages cannot hold a request open.
    """
    return CampaignService(
        session=session,
        tenant_id=workspace.tenant.id,
        # Scheduling is where a campaign's cost is committed, and the limit
        # depends on how many people it will reach.
        entitlements=EntitlementService(
            session,
            tenant_id=workspace.tenant.id,
            default_plan_code=settings.default_plan_code,
        ),
    )


CampaignServiceDep = Annotated[CampaignService, Depends(get_campaign_service)]


def get_follow_up_service(
    settings: SettingsDep,
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> FollowUpService:
    """Workspace-scoped, so no route can pass a tenant id of its own choosing.

    Settings are supplied even though no route dispatches a follow-up, so the
    service a request holds is the same one the worker holds.
    """
    return FollowUpService(
        session=session,
        tenant_id=workspace.tenant.id,
        settings=settings,
    )


FollowUpServiceDep = Annotated[FollowUpService, Depends(get_follow_up_service)]


def get_inbox_service(session: SessionDep, workspace: ActiveWorkspaceDep) -> InboxService:
    """Workspace-scoped, so no route can pass a tenant id of its own choosing."""
    return InboxService(session=session, tenant_id=workspace.tenant.id)


InboxServiceDep = Annotated[InboxService, Depends(get_inbox_service)]


def get_knowledge_service(
    session: SessionDep,
    redis: RedisDep,
    workspace: ActiveWorkspaceDep,
) -> KnowledgeService:
    """Workspace-scoped, so no route can pass a tenant id of its own choosing."""
    return KnowledgeService(
        session=session,
        tenant_id=workspace.tenant.id,
        queue=IngestionQueue(redis.client),
    )


KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]


def get_lead_service(session: SessionDep, workspace: ActiveWorkspaceDep) -> LeadService:
    """Workspace-scoped, so no route can pass a tenant id of its own choosing."""
    return LeadService(session=session, tenant_id=workspace.tenant.id)


LeadServiceDep = Annotated[LeadService, Depends(get_lead_service)]


def get_messaging_service(
    settings: SettingsDep,
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> MessagingService:
    return MessagingService(
        session=session,
        settings=settings,
        tenant_id=workspace.tenant.id,
    )


MessagingServiceDep = Annotated[MessagingService, Depends(get_messaging_service)]


def get_media_storage(settings: SettingsDep) -> MediaStorage:
    """The file store this deployment is configured with.

    Returned as the protocol rather than the concrete class, so replacing the
    local implementation with an object store is a change here and nowhere else
    (ADR-023).
    """
    return LocalMediaStorage(settings.media_storage_path)


MediaStorageDep = Annotated[MediaStorage, Depends(get_media_storage)]


def get_media_service(
    settings: SettingsDep,
    session: SessionDep,
    storage: MediaStorageDep,
    workspace: ActiveWorkspaceDep,
) -> MediaService:
    """Workspace-scoped, so no route can pass a tenant id of its own choosing.

    No WhatsApp client: nothing reachable over the API downloads from Meta.
    That happens in the worker, where it cannot hold a request open.
    """
    return MediaService(
        session=session,
        tenant_id=workspace.tenant.id,
        settings=settings,
        storage=storage,
    )


MediaServiceDep = Annotated[MediaService, Depends(get_media_service)]


def get_sentiment_service(
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> SentimentService:
    """Workspace-scoped, and deliberately without an analyzer.

    Nothing reachable over the API classifies anything. Assessment happens in
    the worker, before an agent replies; what a request needs from this service
    is the one operation a person performs - giving a priority back.
    """
    return SentimentService(session=session, tenant_id=workspace.tenant.id)


SentimentServiceDep = Annotated[SentimentService, Depends(get_sentiment_service)]


def get_template_service(
    settings: SettingsDep,
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> TemplateService:
    """Workspace-scoped, so no route can pass a tenant id of its own choosing.

    Settings are supplied because a sync calls Meta, which is the one thing in
    this service that leaves the process.
    """
    return TemplateService(
        session=session,
        settings=settings,
        tenant_id=workspace.tenant.id,
    )


TemplateServiceDep = Annotated[TemplateService, Depends(get_template_service)]


def get_usage_service(session: SessionDep, workspace: ActiveWorkspaceDep) -> UsageService:
    """Workspace-scoped, and read-only.

    The recorder is deliberately absent: nothing a request does is metered by a
    route. Meters belong to the services that consume something, which is what
    keeps a meter in the same transaction as the work it measures (ADR-027).
    """
    return UsageService(session, tenant_id=workspace.tenant.id)


UsageServiceDep = Annotated[UsageService, Depends(get_usage_service)]


def get_analytics_service(
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> AnalyticsService:
    """Workspace-scoped reporting. Recording happens in the services that act."""
    return AnalyticsService(session, tenant_id=workspace.tenant.id)


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]


def get_audit_log_repository(
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> AuditLogRepository:
    """Workspace-scoped, so no route can read another workspace's trail."""
    return AuditLogRepository(session, tenant_id=workspace.tenant.id)


AuditLogRepositoryDep = Annotated[AuditLogRepository, Depends(get_audit_log_repository)]


def get_platform_audit_log_repository(session: SessionDep) -> PlatformAuditLogRepository:
    """Every entry, including the platform's own. Behind the platform role.

    The entries this exists to show are precisely the ones the people reading
    it generate, which is the point: the platform owner is not exempt.
    """
    return PlatformAuditLogRepository(session)


PlatformAuditLogRepositoryDep = Annotated[
    PlatformAuditLogRepository,
    Depends(get_platform_audit_log_repository),
]


def get_plan_repository(session: SessionDep) -> PlanRepository:
    """The catalogue. Not workspace-scoped, because a plan belongs to nobody."""
    return PlanRepository(session)


PlanRepositoryDep = Annotated[PlanRepository, Depends(get_plan_repository)]


def get_subscription_service(
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> SubscriptionService:
    """Workspace-scoped, so no route can name a tenant of its own choosing."""
    return SubscriptionService(session, tenant_id=workspace.tenant.id)


SubscriptionServiceDep = Annotated[SubscriptionService, Depends(get_subscription_service)]


def get_entitlement_service(
    settings: SettingsDep,
    session: SessionDep,
    workspace: ActiveWorkspaceDep,
) -> EntitlementService:
    """The one authority on what this workspace may do.

    The default plan code comes from settings rather than from a caller: it is
    what a workspace without a subscription is held to, and a route able to name
    it is a route able to grant itself a better plan.
    """
    return EntitlementService(
        session,
        tenant_id=workspace.tenant.id,
        default_plan_code=settings.default_plan_code,
    )


EntitlementServiceDep = Annotated[EntitlementService, Depends(get_entitlement_service)]


def get_invoice_service(session: SessionDep, workspace: ActiveWorkspaceDep) -> InvoiceService:
    """Workspace-scoped, and deliberately without a payment provider.

    Nothing reachable by a workspace collects money. Reading an invoice back is
    database work; issuing and collecting happen in the worker and the platform
    layer.
    """
    return InvoiceService(session, tenant_id=workspace.tenant.id)


InvoiceServiceDep = Annotated[InvoiceService, Depends(get_invoice_service)]


def get_platform_billing_service(session: SessionDep) -> PlatformBillingService:
    """Invoice administration across workspaces, for platform staff only.

    Recording a payment asserts that somebody has seen the money, and voiding
    withdraws a bill. A workspace able to do either pays nothing.
    """
    return PlatformBillingService(session)


PlatformInvoiceServiceDep = Annotated[
    PlatformBillingService,
    Depends(get_platform_billing_service),
]


def get_platform_analytics_service(session: SessionDep) -> PlatformAnalyticsService:
    """Cross-tenant reporting, and the only service here built without a workspace.

    Nothing narrows it, by design: it is the platform view. The routes that use
    it are guarded by the platform-role dependency below, which is a property of
    the user rather than of a membership.
    """
    return PlatformAnalyticsService(session)


PlatformAnalyticsServiceDep = Annotated[
    PlatformAnalyticsService,
    Depends(get_platform_analytics_service),
]


def require_tenant_roles(*roles: TenantRole) -> Callable[[ActiveWorkspace], ActiveWorkspace]:
    """Build a dependency that admits only these workspace roles.

    Authorization is a dependency rather than a check inside the handler, so a
    route cannot be written that forgets it.
    """
    allowed = frozenset(roles)

    def guard(workspace: ActiveWorkspaceDep) -> ActiveWorkspace:
        if workspace.role not in allowed:
            raise PermissionDeniedError("This action requires a different role in this workspace.")
        return workspace

    return guard


def require_entitlement(
    key: LimitKey,
) -> Callable[[EntitlementService], Awaitable[Entitlement]]:
    """Build a dependency that refuses an action the plan does not allow.

    Shaped exactly like `require_tenant_roles`, and for the same reason: a check
    written inside a handler is a check the next handler forgets. Declaring it in
    the signature means the route cannot run without it.

    It is a limit on *creating* something, so it belongs only on the routes that
    create. Nothing here is on the inbound path - see ADR-030 for why a customer's
    message is never refused for a business's billing.
    """

    async def guard(entitlements: EntitlementServiceDep) -> Entitlement:
        return await entitlements.require(key)

    return guard


AgentSlotDep = Annotated[Entitlement, Depends(require_entitlement(LimitKey.AGENTS))]
NumberSlotDep = Annotated[Entitlement, Depends(require_entitlement(LimitKey.WHATSAPP_NUMBERS))]
SeatDep = Annotated[Entitlement, Depends(require_entitlement(LimitKey.TEAM_MEMBERS))]
DocumentSlotDep = Annotated[
    Entitlement,
    Depends(require_entitlement(LimitKey.KNOWLEDGE_DOCUMENTS)),
]


def require_platform_roles(*roles: PlatformRole) -> Callable[[CurrentUser], CurrentUser]:
    """Build a dependency for platform administration.

    Platform authority is deliberately separate from workspace roles: owning a
    workspace grants nothing across the platform.
    """
    allowed = frozenset(roles)

    def guard(current_user: CurrentUserDep) -> CurrentUser:
        if current_user.user.platform_role not in allowed:
            raise PermissionDeniedError("This action requires platform administration rights.")
        return current_user

    return guard


TenantOwnerDep = Annotated[
    ActiveWorkspace,
    Depends(require_tenant_roles(TenantRole.TENANT_OWNER)),
]
TenantAdminDep = Annotated[
    ActiveWorkspace,
    Depends(require_tenant_roles(TenantRole.TENANT_OWNER, TenantRole.TENANT_ADMIN)),
]
PlatformStaffDep = Annotated[
    CurrentUser,
    Depends(require_platform_roles(PlatformRole.PLATFORM_OWNER, PlatformRole.PLATFORM_ADMIN)),
]
PlatformOwnerDep = Annotated[
    CurrentUser,
    Depends(require_platform_roles(PlatformRole.PLATFORM_OWNER)),
]
