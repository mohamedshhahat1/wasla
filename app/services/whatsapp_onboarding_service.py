"""WhatsApp Embedded Signup: the second way to connect a number (ADR-204).

The assisted form takes a token the workspace pasted. Embedded Signup takes a
five-minute code Meta's popup handed the browser, and the server does the rest,
in the order Meta's Tech Provider page gives it (S4) - each step built on a rule
this codebase already has:

1. **Ask before Meta** (EX-02). Enabled, Meta's weekly onboarding limit, then the
   plan and the channel slot exactly as the assisted connect asks them
   (ADR-131): a workspace that cannot hold the number never makes Wasla call
   Meta, and a refusal here spends nothing - not even the code.
2. **Exchange the code** at once, server to server (EX-03, EX-04).
3. **Prove the number** with the exchanged token through the same
   `OwnershipVerifier` the assisted connect uses (EX-08, ADR-037). The ids the
   popup sent are assertions, checked like a pasted WABA id; a mismatch is the
   same one refusal with the same one sentence.
4. **Read the platform** (CX-01): Meta, not the browser, says whether the
   number is also on the WhatsApp Business app (Coexistence).
5. **Write the connection** under the workspace's lock through
   `WhatsAppAccountService.claim_verified` - the assisted connect's own path -
   with `onboarding_method = embedded_signup` (EX-09). A number this workspace
   already holds is proven again instead (EX-11, RL-05, ADR-041).
6. **After the commit** (SB-03): subscribe the app to the customer's webhooks,
   and register a new Cloud API number with a PIN Wasla generated and sealed -
   never a Coexistence number, which is already registered (SB-10). A failure
   here leaves the connection written and says so in its health, with a retry
   from the connection page (SB-04, SB-11).

Coexistence's two one-time syncs are owed from step 5 on: the row records the
24-hour deadline in the same transaction, and `OnboardingWorker` requests them
(SY-01).

Nothing here returns, logs or stores unsealed the code, the business token or
the PIN.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import CONNECTION_BOUND, CredentialCipher
from app.core.events import RealtimeEvent, stage
from app.core.exceptions import ConflictError, ExternalServiceError, WaslaError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.channel import Channel, ConnectionHealth
from app.db.models.user import User
from app.db.models.whatsapp import (
    OnboardingMethod,
    OnboardingMode,
    WhatsAppAccount,
    WhatsAppAccountStatus,
)
from app.integrations.whatsapp.onboarding import (
    NumberPlatform,
    OnboardingClient,
    OnboardingStepError,
)
from app.integrations.whatsapp.ownership import OwnershipVerifier
from app.repositories.channel_repository import ChannelConnectionRepository
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.services.audit_service import AuditTrail
from app.services.credential_service import CredentialService, connection_context
from app.services.whatsapp_account_service import WhatsAppAccountService

logger = get_logger(__name__)

#: Meta: "a fixed throughput of 20 mps" for a Coexistence number (EXT-ES-21).
COEXISTENCE_SENDS_PER_MINUTE: Final = 20 * 60
#: Meta's deadline for the contacts and history syncs (EXT-ES-23).
SYNC_DEADLINE: Final = timedelta(hours=24)
#: The rolling window of Meta's onboarding limit (EXT-ES-18).
ONBOARDING_WINDOW: Final = timedelta(days=7)
#: Bound like the token, under its own context so neither opens the other.
PIN_CONTEXT_SUFFIX: Final = ":registration_pin"
PIN_DIGITS: Final = 6
#: The stream event the connection page refreshes on (OS-05).
ONBOARDING_EVENT: Final = "whatsapp.onboarding.updated"


class EmbeddedSignupDisabledError(WaslaError):
    """This deployment cannot finish an Embedded Signup (CF-04)."""

    status_code = 409
    error_code = "embedded_signup_disabled"
    message = "Connecting with Meta is not available. Use the access-token form."


class EmbeddedSignupOnboardingLimitError(WaslaError):
    """Meta's limit on new WhatsApp accounts in a rolling 7 days is reached (OQ-12)."""

    status_code = 409
    error_code = "embedded_signup_onboarding_limit"
    message = (
        "New connections are paused for a few days. Use the access-token form or try again later."
    )


class EmbeddedSignupCoexistenceUnavailableError(WaslaError):
    """The number is on the WhatsApp Business app and this deployment does not offer it."""

    status_code = 409
    error_code = "embedded_signup_coexistence_unavailable"
    message = (
        "This number is used in the WhatsApp Business app, which this deployment "
        "cannot connect. Use a new number."
    )


@dataclass(frozen=True, slots=True)
class EmbeddedSignupRequest:
    """What the browser sent: a code, and session ids that are only assertions."""

    code: str
    phone_number_id: str
    #: v5's `messaging_account_ids[0]`, or v4's `waba_id` (EXT-ES-03 / 04).
    messaging_account_id: str
    business_id: str | None = None
    #: v5's `is_on_biz_app`, or the FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING
    #: event: a hint, recorded and compared, never used (CX-02, CX-05).
    is_on_biz_app_hint: bool | None = None


@dataclass(slots=True)
class OnboardingResult:
    account: WhatsAppAccount
    replayed: bool
    #: What is still to do with Meta once the transaction has committed.
    #: Holds the token in memory only, for the length of the request.
    complete: Callable[[], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class ReleaseFollowUp:
    """The best-effort unsubscribe a release of an Embedded Signup number owes (RL-02)."""

    token: str
    messaging_account_id: str

    def __repr__(self) -> str:  # pragma: no cover - keeps the token out of any log
        return "ReleaseFollowUp(<redacted>)"


def new_registration_pin() -> str:
    """Six digits from the operating system's secure generator (SB-08)."""
    return "".join(str(secrets.randbelow(10)) for _ in range(PIN_DIGITS))


class WhatsAppOnboardingService:
    """Every method takes the workspace explicitly, as the account service does."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        accounts: WhatsAppAccountService,
        ownership: OwnershipVerifier | None,
        client: OnboardingClient | None,
        credentials: CredentialService,
        cipher: CredentialCipher | None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._accounts = accounts
        self._ownership = ownership
        self._client = client
        self._credentials = credentials
        self._cipher = cipher

    # ------------------------------------------------------------------ config

    @property
    def enabled(self) -> bool:
        return (
            self._settings.embedded_signup_enabled
            and self._client is not None
            and self._ownership is not None
            and self._cipher is not None
        )

    # -------------------------------------------------------------- onboarding

    async def onboard(
        self,
        *,
        tenant_id: uuid.UUID,
        request: EmbeddedSignupRequest,
        idempotency_key: str,
        actor: User | None = None,
    ) -> OnboardingResult:
        """Exchange, prove, write. Meta is called only after every local question (EX-02)."""
        if not self.enabled or self._client is None or self._ownership is None:
            raise EmbeddedSignupDisabledError()

        replay = await self._by_idempotency_key(tenant_id, idempotency_key)
        if replay is not None:
            # The first request's connection, and no call to Meta (EX-12).
            return OnboardingResult(account=replay, replayed=True)

        await self._check_weekly_limit()
        guard = self._accounts.capacity_guard(tenant_id)
        held = await self._held_here(tenant_id, request.phone_number_id.strip())
        if held is None:
            await guard.precheck(Channel.WHATSAPP)

        exchanged = await self._client.exchange_code(request.code)
        token = exchanged.access_token
        verified = await self._ownership.verify(
            access_token=token,
            phone_number_id=request.phone_number_id.strip(),
            claimed_waba_id=request.messaging_account_id.strip() or None,
        )
        try:
            platform = await self._client.read_platform(
                token=token, phone_number_id=verified.phone_number_id
            )
        except OnboardingStepError as error:
            # The mode decides registration; guessing it is not allowed (CX-06).
            raise ExternalServiceError(
                "Meta did not say which WhatsApp this number is on. Try again."
            ) from error
        self._compare_hint(request, platform)
        if platform.coexistence and not self._settings.meta_embedded_signup_coexistence:
            raise EmbeddedSignupCoexistenceUnavailableError()

        limits = self._accounts.messaging_limits
        limit = (
            await limits.read_quietly(access_token=token, phone_number_id=verified.phone_number_id)
            if limits is not None
            else None
        )

        now = datetime.now(UTC)
        meta = {
            "onboarding_method": OnboardingMethod.EMBEDDED_SIGNUP.value,
            "onboarding_mode": (
                OnboardingMode.COEXISTENCE.value
                if platform.coexistence
                else OnboardingMode.CLOUD_API.value
            ),
        }
        if held is not None:
            account = await self._relink(
                held, verified_waba=verified.waba_id, token=token, actor=actor
            )
            account.display_phone_number = verified.display_phone_number
            account.verified_name = verified.verified_name
        else:
            account = await self._accounts.claim_verified(
                tenant_id=tenant_id,
                verified=verified,
                access_token=token,
                limit=limit,
                guard=guard,
                actor=actor,
                audit_meta=meta,
            )

        pin = None if platform.coexistence else new_registration_pin()
        self._stamp(account, platform=platform, key=idempotency_key, now=now, pin=pin)
        connection = await ChannelConnectionRepository(
            self._session, tenant_id=tenant_id
        ).require_by_id(account.id)
        if platform.coexistence:
            connection.sends_per_minute = COEXISTENCE_SENDS_PER_MINUTE
        await self._session.flush()
        await self._session.refresh(account)
        self._announce(account)
        logger.info(
            "whatsapp.embedded_signup_onboarded",
            extra={
                "event": "whatsapp.embedded_signup_onboarded",
                "phone_number_id": account.phone_number_id,
                "mode": account.onboarding_mode,
                "relinked": held is not None,
            },
        )

        account_id = account.id

        async def complete() -> None:
            await self.finish(tenant_id=tenant_id, account_id=account_id, token=token, pin=pin)

        return OnboardingResult(account=account, replayed=False, complete=complete)

    async def finish(
        self,
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        token: str,
        pin: str | None,
    ) -> WhatsAppAccount:
        """Subscribe, then register a Cloud API number. Never undoes the connection (SB-03)."""
        if self._client is None:  # pragma: no cover - `enabled` refuses first
            raise EmbeddedSignupDisabledError()
        account = await WhatsAppAccountRepository(
            self._session, tenant_id=tenant_id
        ).require_live_by_id(account_id)
        connection = await ChannelConnectionRepository(
            self._session, tenant_id=tenant_id
        ).require_by_id(account.id)
        messaging_account = account.messaging_account_id or account.waba_id
        health: tuple[ConnectionHealth, str] | None = None

        if account.subscribed_at is None:
            try:
                await self._client.subscribe(token=token, messaging_account_id=messaging_account)
                account.subscribed_at = datetime.now(UTC)
            except OnboardingStepError as error:
                health = (
                    (ConnectionHealth.AUTH_FAILED, "subscribe")
                    if error.is_auth
                    else (ConnectionHealth.WEBHOOK_UNHEALTHY, "subscribe")
                )

        if not account.is_coexistence and account.registered_at is None:
            stored_pin = pin or self._open_pin(account)
            if stored_pin is not None:
                try:
                    await self._client.register(
                        token=token, phone_number_id=account.phone_number_id, pin=stored_pin
                    )
                    account.registered_at = datetime.now(UTC)
                except OnboardingStepError as error:
                    if health is None:
                        health = (
                            (ConnectionHealth.AUTH_FAILED, "register")
                            if error.is_auth
                            else (ConnectionHealth.WEBHOOK_UNHEALTHY, "register")
                        )

        if health is not None:
            if connection.health is not health[0] or connection.health_reason != health[1]:
                connection.health = health[0]
                connection.health_reason = health[1]
                connection.health_changed_at = datetime.now(UTC)
        elif connection.health in (
            ConnectionHealth.WEBHOOK_UNHEALTHY,
            ConnectionHealth.AUTH_FAILED,
        ):
            connection.health = ConnectionHealth.OK
            connection.health_reason = None
            connection.health_changed_at = datetime.now(UTC)
        await self._session.flush()
        self._announce(account)
        return account

    async def retry_setup(self, *, tenant_id: uuid.UUID, account_id: uuid.UUID) -> WhatsAppAccount:
        """Retry whichever of subscribe and register is missing, with the stored token (SB-04)."""
        if self._client is None:
            raise EmbeddedSignupDisabledError()
        account = await WhatsAppAccountRepository(
            self._session, tenant_id=tenant_id
        ).require_live_by_id(account_id)
        if not account.is_embedded_signup:
            raise ConflictError("This number was not connected with Meta.")
        if account.status is not WhatsAppAccountStatus.ACTIVE:
            raise ConflictError("Enable the number before retrying its setup.")
        token = self._credentials.resolve(account)
        if not token.is_own:
            raise ConflictError("The number's credential is not stored. Connect it again.")
        return await self.finish(
            tenant_id=tenant_id, account_id=account.id, token=token.token, pin=None
        )

    # ----------------------------------------------------------------- release

    async def unsubscribe(self, follow_up: ReleaseFollowUp) -> bool:
        """Best-effort, after the release commits; never undoes it (RL-02, RL-03).

        The Deregister API is never called - not for Coexistence (Meta: it
        cannot be used), not for any other number (OQ-6, RL-04).
        """
        if self._client is None:
            return False
        try:
            await self._client.unsubscribe(
                token=follow_up.token, messaging_account_id=follow_up.messaging_account_id
            )
        except OnboardingStepError:
            logger.warning(
                "whatsapp.unsubscribe_failed",
                extra={"event": "whatsapp.unsubscribe_failed", "es_unsubscribe_total": "failure"},
            )
            return False
        logger.info(
            "whatsapp.unsubscribed",
            extra={"event": "whatsapp.unsubscribed", "es_unsubscribe_total": "success"},
        )
        return True

    # ------------------------------------------------------------------ helpers

    async def _by_idempotency_key(self, tenant_id: uuid.UUID, key: str) -> WhatsAppAccount | None:
        found = await self._session.execute(
            select(WhatsAppAccount).where(
                WhatsAppAccount.tenant_id == tenant_id,
                WhatsAppAccount.onboarding_idempotency_key == key,
            )
        )
        return found.scalar_one_or_none()

    async def _check_weekly_limit(self) -> None:
        """Wasla's own successful onboardings, platform-wide, in Meta's 7 days (OQ-12)."""
        since = datetime.now(UTC) - ONBOARDING_WINDOW
        count = (
            await self._session.execute(
                select(func.count())
                .select_from(WhatsAppAccount)
                .where(
                    WhatsAppAccount.onboarding_method == OnboardingMethod.EMBEDDED_SIGNUP.value,
                    WhatsAppAccount.onboarded_at >= since,
                )
            )
        ).scalar_one()
        if count >= self._settings.meta_onboarding_weekly_limit:
            raise EmbeddedSignupOnboardingLimitError()

    async def _held_here(
        self, tenant_id: uuid.UUID, phone_number_id: str
    ) -> WhatsAppAccount | None:
        """This workspace's live claim on the number, if any: a re-link, not a new claim."""
        found = await self._session.execute(
            select(WhatsAppAccount).where(
                WhatsAppAccount.tenant_id == tenant_id,
                WhatsAppAccount.phone_number_id == phone_number_id,
                WhatsAppAccount.released_at.is_(None),
            )
        )
        return found.scalar_one_or_none()

    async def _relink(
        self,
        account: WhatsAppAccount,
        *,
        verified_waba: str,
        token: str,
        actor: User | None,
    ) -> WhatsAppAccount:
        """Prove again a number this workspace holds, and bring it back (EX-11, RL-05).

        The re-verification path of ADR-041: the stored business account is
        Meta's answer, the token is replaced, and a number Meta disconnected
        is enabled again - which takes a channel slot like any enable (ENT-07).
        """
        tenant_id = account.tenant_id
        previous = account.waba_id
        account.waba_id = verified_waba
        account.ownership_verified_at = datetime.now(UTC)
        account.access_token_encrypted = self._credentials.seal(
            token, tenant_id=tenant_id, connection_id=account.id, channel=Channel.WHATSAPP
        )
        account.provider_disconnect_reason = None
        account.provider_disconnect_initiated_by = None
        account.provider_disconnected_at = None
        account.subscribed_at = None
        account.registered_at = None
        for column in (
            "contacts_sync_requested_at",
            "contacts_sync_request_id",
            "history_sync_requested_at",
            "history_sync_request_id",
            "history_progress",
            "history_phase_max",
            "history_completed_at",
            "history_declined_at",
            "sync_next_attempt_at",
        ):
            setattr(account, column, None)
        account.contacts_sync_attempts = 0
        account.history_sync_attempts = 0
        connection = await ChannelConnectionRepository(
            self._session, tenant_id=tenant_id
        ).require_by_id(account.id)
        if account.status is not WhatsAppAccountStatus.ACTIVE:
            await self._accounts.capacity_guard(tenant_id).reserve_or_refuse(Channel.WHATSAPP)
            account.status = WhatsAppAccountStatus.ACTIVE
            connection.disabled_reason = None
            connection.disabled_at = None
            connection.disabled_by = None
        if connection.health is ConnectionHealth.PROVIDER_DISCONNECTED:
            connection.health = ConnectionHealth.OK
            connection.health_reason = None
            connection.health_changed_at = datetime.now(UTC)
        await self._session.flush()
        AuditTrail(self._session, tenant_id=tenant_id).record(
            AuditAction.WHATSAPP_ACCOUNT_VERIFIED,
            actor=actor,
            actor_kind=AuditActorKind.USER if actor is not None else AuditActorKind.SYSTEM,
            target_type="whatsapp_account",
            target_id=account.id,
            target_label=account.display_phone_number,
            meta={
                "waba_id": account.waba_id,
                "previous_waba_id": previous,
                "waba_changed": previous != account.waba_id,
                "onboarding_method": OnboardingMethod.EMBEDDED_SIGNUP.value,
            },
        )
        return account

    def _stamp(
        self,
        account: WhatsAppAccount,
        *,
        platform: NumberPlatform,
        key: str,
        now: datetime,
        pin: str | None,
    ) -> None:
        account.onboarding_method = OnboardingMethod.EMBEDDED_SIGNUP.value
        account.onboarding_mode = (
            OnboardingMode.COEXISTENCE.value
            if platform.coexistence
            else OnboardingMode.CLOUD_API.value
        )
        account.messaging_account_id = account.waba_id
        account.onboarding_idempotency_key = key
        account.onboarded_at = now
        if platform.coexistence:
            account.sync_deadline_at = now + SYNC_DEADLINE
            account.sync_next_attempt_at = now
            account.registration_pin_encrypted = None
        else:
            account.sync_deadline_at = None
            account.sync_next_attempt_at = None
            account.registration_pin_encrypted = (
                self._seal_pin(account, pin) if pin is not None else None
            )

    def _seal_pin(self, account: WhatsAppAccount, pin: str) -> str:
        if self._cipher is None:  # pragma: no cover - `enabled` requires a key
            raise EmbeddedSignupDisabledError()
        return self._cipher.encrypt(pin, context=_pin_context(account), version=CONNECTION_BOUND)

    def _open_pin(self, account: WhatsAppAccount) -> str | None:
        if self._cipher is None or account.registration_pin_encrypted is None:
            return None
        return self._cipher.decrypt(
            account.registration_pin_encrypted, context=_pin_context(account)
        )

    @staticmethod
    def _compare_hint(request: EmbeddedSignupRequest, platform: NumberPlatform) -> None:
        if request.is_on_biz_app_hint is None:
            return
        if request.is_on_biz_app_hint != platform.coexistence:
            # Meta's read wins; the disagreement is counted (CX-05).
            logger.warning(
                "whatsapp.es_coexistence_hint_mismatch",
                extra={"event": "whatsapp.es_coexistence_hint_mismatch"},
            )

    def _announce(self, account: WhatsAppAccount) -> None:
        stage(
            self._session.sync_session,
            RealtimeEvent(
                tenant_id=account.tenant_id,
                type=ONBOARDING_EVENT,
                data={"connection_id": str(account.id)},
            ),
        )


def _pin_context(account: WhatsAppAccount) -> str:
    return (
        connection_context(
            tenant_id=account.tenant_id, connection_id=account.id, channel=Channel.WHATSAPP
        )
        + PIN_CONTEXT_SUFFIX
    )


def announce_onboarding(session: AsyncSession, account: WhatsAppAccount) -> None:
    """The stream hint every onboarding change sends (OS-05), for callers outside the service."""
    stage(
        session.sync_session,
        RealtimeEvent(
            tenant_id=account.tenant_id,
            type=ONBOARDING_EVENT,
            data={"connection_id": str(account.id)},
        ),
    )


__all__ = [
    "COEXISTENCE_SENDS_PER_MINUTE",
    "ONBOARDING_EVENT",
    "SYNC_DEADLINE",
    "EmbeddedSignupCoexistenceUnavailableError",
    "EmbeddedSignupDisabledError",
    "EmbeddedSignupOnboardingLimitError",
    "EmbeddedSignupRequest",
    "OnboardingResult",
    "ReleaseFollowUp",
    "WhatsAppOnboardingService",
    "announce_onboarding",
    "new_registration_pin",
]
