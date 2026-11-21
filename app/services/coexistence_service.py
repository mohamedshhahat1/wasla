"""WhatsApp Coexistence after onboarding: history, contacts, syncs and disconnects (ADR-204).

A number used in the WhatsApp Business app *and* through Wasla (Meta's
"Onboard WhatsApp Business app users", S5) sends four things no Cloud API
number sends. Each is handled here, by rules decided once:

- **`history`** (HI): chunks of up to 180 days of the app's chats, stored as
  received by the webhook and projected here by the onboarding worker into the
  conversations as *imported* lines. An imported line is never answered,
  metered, counted against an allowance, never opens, extends or moves a
  service window, never cancels a follow-up, opts anybody out or notifies
  anybody - it is the past, shown. Idempotent by message id per connection
  (ADR-120), so chunks arriving twice or out of order change nothing.
- **`smb_app_state_sync`** (CT): the app's address book. A name and the
  missing phone / business-scoped id are attached only to a contact Wasla
  already holds or that imported history created, and only a name where the
  contact has none (OQ-4). Nobody is created from an address book; a `remove`
  removes nothing.
- **`account_update`** (AU): Meta saying the partner was removed, the account
  offboarded or the app uninstalled. The connection is disabled with reason
  `provider_disconnected` exactly as any disable (ENT-07): the token and the
  PIN dropped, follow-ups and syncs stopped, the slot freed, the conversations
  kept. Nothing re-enables it but a new onboarding (RL-05).
- **the two one-time syncs** (SY): the contacts and the history are each
  requested once, inside Meta's 24 hours, by `request_due_syncs`; a request Meta
  accepted is never sent again.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.inbound import Identifier
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionDisabledReason,
    ConnectionHealth,
    IdentityKind,
)
from app.db.models.channel_event import ChannelEvent, ChannelEventKind, ChannelEventState
from app.db.models.conversation import MessageKind, MessageStatus
from app.db.models.whatsapp import OnboardingMode, WhatsAppAccount, WhatsAppAccountStatus
from app.integrations.whatsapp.adapter import MESSAGE_KINDS, WhatsAppAdapter
from app.integrations.whatsapp.onboarding import (
    SYNC_CONTACTS,
    SYNC_HISTORY,
    OnboardingClient,
    OnboardingStepError,
)
from app.integrations.whatsapp.payload import (
    HISTORY_FIELD,
    MAX_PHONE_LENGTH,
    AccountUpdateEvent,
    CoexistenceDelivery,
    message_text,
)
from app.repositories.channel_event_repository import ChannelEventRepository
from app.repositories.channel_repository import (
    ChannelConnectionRepository,
    ContactIdentityRepository,
)
from app.repositories.conversation_repository import ContactRepository
from app.services.audit_service import AuditTrail
from app.services.channel_connection_service import stop_automation
from app.services.contact_identity_service import ContactIdentityService
from app.services.conversation_service import ConversationProjectionService, ProjectionOutcome
from app.services.credential_service import CredentialService
from app.services.whatsapp_onboarding_service import announce_onboarding

logger = get_logger(__name__)

#: Meta's word for a history share the business turned off (EXT-ES-25).
HISTORY_DECLINED_CODE: Final = 2593109
#: The account_update words that take the number away (AU-03, EXT-ES-35).
DISCONNECTING_EVENTS: Final = frozenset(
    {"PARTNER_REMOVED", "ACCOUNT_OFFBOARDED", "PARTNER_APP_UNINSTALLED"}
)
#: Recorded and acted on in no other way (AU-06).
RECORDED_EVENTS: Final = frozenset({"PARTNER_ADDED", "ACCOUNT_RECONNECTED"})
#: history_context.status onto the statuses Wasla keeps (HI-10).
HISTORY_STATUSES: Final[Mapping[str, MessageStatus]] = {
    "sent": MessageStatus.SENT,
    "pending": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "played": MessageStatus.READ,
    "error": MessageStatus.FAILED,
}
#: How long an address-book entry waits for imported history to create its
#: contact before it is settled unapplied (CT-03).
CONTACTS_WAIT: Final = timedelta(days=3)
#: The sync retry backoff (SY-04): doubling from a minute, at most half an hour.
SYNC_BACKOFF_START: Final = timedelta(minutes=1)
SYNC_BACKOFF_MAX: Final = timedelta(minutes=30)
_DIGITS: Final = re.compile(r"\D")
_PHONE_LIKE: Final = re.compile(r"^\+?\d{5,15}$")


def _digits(value: str | None) -> str:
    return _DIGITS.sub("", value or "")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, str) else []


def _text(value: Any, limit: int = 255) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------- the webhook


async def _live_account(session: AsyncSession, phone_number_id: str) -> WhatsAppAccount | None:
    found = await session.execute(
        select(WhatsAppAccount).where(
            WhatsAppAccount.phone_number_id == phone_number_id,
            WhatsAppAccount.released_at.is_(None),
        )
    )
    return found.scalar_one_or_none()


async def store_coexistence_deliveries(
    session: AsyncSession, deliveries: Sequence[CoexistenceDelivery]
) -> Counter[str]:
    """Capture history and contacts deliveries as received; the worker projects them (HI-01).

    A delivery for a number nobody holds is counted and dropped; a replay of a
    stored one is the same event (its id is a digest) and is stored once.
    """
    counts: Counter[str] = Counter()
    for delivery in deliveries:
        account = await _live_account(session, delivery.phone_number_id)
        if account is None or account.status is not WhatsAppAccountStatus.ACTIVE:
            counts["unknown_connection"] += 1
            continue
        kind = (
            ChannelEventKind.HISTORY
            if delivery.field == HISTORY_FIELD
            else ChannelEventKind.STATE_SYNC
        )
        stored = await ChannelEventRepository(session, tenant_id=account.tenant_id).store(
            connection_id=account.id,
            channel=Channel.WHATSAPP,
            event_id=delivery.event_id,
            kind=kind,
            payload=delivery.value,
            received_at=delivery.at or datetime.now(UTC),
        )
        counts["stored" if stored.created else "duplicate"] += 1
    if deliveries:
        logger.info(
            "whatsapp.coexistence_delivery",
            extra={"event": "whatsapp.coexistence_delivery", **dict(counts)},
        )
    return counts


async def apply_account_updates(
    session: AsyncSession, events: Sequence[AccountUpdateEvent]
) -> Counter[str]:
    """Meta's `account_update`, applied to every number on the Messaging account it names (AU)."""
    counts: Counter[str] = Counter()
    for update in events:
        # es_account_update_total{event} (AU-02).
        counts[update.event] += 1
        accounts = (
            (
                await session.execute(
                    select(WhatsAppAccount).where(
                        WhatsAppAccount.released_at.is_(None),
                        or_(
                            WhatsAppAccount.waba_id == update.waba_id,
                            WhatsAppAccount.messaging_account_id == update.waba_id,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not accounts:
            counts["unknown_account"] += 1
            continue
        for account in accounts:
            stored = await ChannelEventRepository(session, tenant_id=account.tenant_id).store(
                connection_id=account.id,
                channel=Channel.WHATSAPP,
                event_id=update.event_id,
                kind=ChannelEventKind.ACCOUNT_UPDATE,
                payload={
                    "event": update.event,
                    "waba_id": update.waba_id,
                    "reason": update.reason,
                    "initiated_by": update.initiated_by,
                },
                received_at=update.at or datetime.now(UTC),
            )
            if not stored.created or stored.event is None:
                # The same event again disables once (AU-11).
                continue
            if update.event in DISCONNECTING_EVENTS:
                await _disconnect(session, account, update)
            ChannelEventRepository(session, tenant_id=account.tenant_id).mark_processed(
                stored.event
            )
    if events:
        logger.info(
            "whatsapp.account_update",
            extra={"event": "whatsapp.account_update", "es_account_update_total": dict(counts)},
        )
    return counts


async def _disconnect(
    session: AsyncSession, account: WhatsAppAccount, update: AccountUpdateEvent
) -> None:
    """Meta took the number away: ENT-07's disable, with Meta's reason (AU-03 .. AU-05)."""
    if (
        account.status is WhatsAppAccountStatus.DISABLED
        and account.provider_disconnected_at is not None
    ):
        return
    now = datetime.now(UTC)
    connection = await ChannelConnectionRepository(
        session, tenant_id=account.tenant_id
    ).require_by_id(account.id)
    reason = update.reason or update.event
    account.status = WhatsAppAccountStatus.DISABLED
    account.access_token_encrypted = None
    account.registration_pin_encrypted = None
    account.provider_disconnect_reason = reason[:64]
    account.provider_disconnect_initiated_by = (update.initiated_by or "")[:16] or None
    account.provider_disconnected_at = now
    account.sync_next_attempt_at = None
    connection.disabled_reason = ConnectionDisabledReason.PROVIDER_DISCONNECTED
    connection.disabled_at = now
    connection.disabled_by = None
    connection.health = ConnectionHealth.PROVIDER_DISCONNECTED
    connection.health_reason = reason[:200]
    connection.health_changed_at = now
    await session.flush()
    await stop_automation(session, tenant_id=account.tenant_id, connection_id=account.id)
    AuditTrail(session, tenant_id=account.tenant_id).record(
        AuditAction.WHATSAPP_ACCOUNT_DISABLED,
        actor=None,
        actor_kind=AuditActorKind.SYSTEM,
        target_type="whatsapp_account",
        target_id=account.id,
        target_label=account.display_phone_number,
        meta={
            "reason": ConnectionDisabledReason.PROVIDER_DISCONNECTED.value,
            "provider_event": update.event,
            "provider_reason": update.reason,
            "initiated_by": update.initiated_by,
        },
    )
    announce_onboarding(session, account)
    logger.warning(
        "whatsapp.provider_disconnected",
        extra={
            "event": "whatsapp.provider_disconnected",
            "phone_number_id": account.phone_number_id,
            "provider_event": update.event,
            "reason": update.reason,
        },
    )


# -------------------------------------------------------- the projection pass


@dataclass
class ProjectionTally:
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    contacts_applied: int = 0
    contacts_waiting: int = 0
    events: int = 0
    reasons: Counter[str] = field(default_factory=Counter)


class CoexistenceProjector:
    """Projects stored history and contacts deliveries, one transaction per pass."""

    def __init__(self, session: AsyncSession, *, now: datetime | None = None) -> None:
        self._session = session
        self._adapter = WhatsAppAdapter()
        self._now = now or datetime.now(UTC)

    async def run(self, *, limit: int = 50) -> ProjectionTally:
        """History first, then contacts - so an address-book entry finds its contact (CT-03)."""
        tally = ProjectionTally()
        for kind in (ChannelEventKind.HISTORY, ChannelEventKind.STATE_SYNC):
            events = (
                (
                    await self._session.execute(
                        select(ChannelEvent)
                        .where(
                            ChannelEvent.kind == kind,
                            ChannelEvent.state == ChannelEventState.RECEIVED,
                        )
                        .order_by(ChannelEvent.created_at, ChannelEvent.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for event in events:
                await self._project(event, tally)
        return tally

    async def _project(self, event: ChannelEvent, tally: ProjectionTally) -> None:
        tally.events += 1
        repository = ChannelEventRepository(self._session, tenant_id=event.tenant_id)
        account = await self._session.get(WhatsAppAccount, event.account_id)
        connection = await self._session.get(ChannelConnection, event.account_id)
        if account is None or connection is None or event.payload is None:
            repository.mark_processed(event)
            return
        if account.released_at is not None or account.status is not WhatsAppAccountStatus.ACTIVE:
            # Released or disconnected: kept as evidence, never projected.
            repository.mark_processed(event)
            return
        if event.kind is ChannelEventKind.HISTORY:
            await self._history(account, connection, event.payload, tally)
            repository.mark_processed(event)
            announce_onboarding(self._session, account)
            return
        settled = await self._contacts(account, connection, event.payload, tally)
        if settled:
            repository.mark_processed(event)

    # ------------------------------------------------------------------ history

    async def _history(
        self,
        account: WhatsAppAccount,
        connection: ChannelConnection,
        value: Mapping[str, Any],
        tally: ProjectionTally,
    ) -> None:
        business = _digits(account.display_phone_number) or _digits(
            _mapping(value.get("metadata")).get("display_phone_number")
        )
        for raw_item in _sequence(value.get("history")):
            item = _mapping(raw_item)
            for raw_error in _sequence(item.get("errors")):
                if _mapping(raw_error).get("code") == HISTORY_DECLINED_CODE:
                    # The business chose not to share; nothing is retried (HI-04).
                    if account.history_declined_at is None:
                        account.history_declined_at = self._now
                    tally.reasons["declined"] += 1
            metadata = _mapping(item.get("metadata"))
            self._progress(account, metadata)
            for raw_thread in _sequence(item.get("threads")):
                await self._thread(account, connection, _mapping(raw_thread), business, tally)

    def _progress(self, account: WhatsAppAccount, metadata: Mapping[str, Any]) -> None:
        progress = metadata.get("progress")
        phase = metadata.get("phase")
        if isinstance(progress, int) and not isinstance(progress, bool) and 0 <= progress <= 100:
            account.history_progress = max(account.history_progress or 0, progress)
            if progress == 100 and account.history_completed_at is None:
                account.history_completed_at = self._now
        if isinstance(phase, int) and not isinstance(phase, bool) and 0 <= phase <= 2:
            account.history_phase_max = max(
                account.history_phase_max if account.history_phase_max is not None else -1, phase
            )

    async def _thread(
        self,
        account: WhatsAppAccount,
        connection: ChannelConnection,
        thread: Mapping[str, Any],
        business: str,
        tally: ProjectionTally,
    ) -> None:
        context = _mapping(thread.get("context"))
        phone = _text(context.get("wa_id"), MAX_PHONE_LENGTH) or _text(
            thread.get("id"), MAX_PHONE_LENGTH
        )
        user_id = _text(context.get("user_id"))
        identifiers: list[Identifier] = []
        if phone is not None and _PHONE_LIKE.match(phone):
            identifiers.append(Identifier(kind=IdentityKind.PHONE, value=phone))
        if user_id is not None:
            identifiers.append(Identifier(kind=IdentityKind.BSUID, value=user_id))
        if not identifiers:
            # Not a person (groups are not delivered, S5): counted, skipped (HI-12).
            tally.skipped += 1
            tally.reasons["not_a_person"] += 1
            return
        messages = [_mapping(raw) for raw in _sequence(thread.get("messages"))]
        if not messages:
            return
        scopes = {
            identifier: await self._adapter.identity_scope(self._session, connection, identifier)
            for identifier in identifiers
        }
        newest = max(
            (_timestamp(message.get("timestamp")) for message in messages),
            key=lambda moment: moment or datetime.min.replace(tzinfo=UTC),
        )
        sender = await ContactIdentityService(
            session=self._session, tenant_id=account.tenant_id
        ).resolve_sender(
            connection=connection,
            identifiers=identifiers,
            scopes=scopes,
            participant_preference=self._adapter.participant_preference,
            anchor_preference=self._adapter.anchor_preference,
            profile_name=None,
            seen_at=newest or self._now,
        )
        projection = ConversationProjectionService(
            session=self._session, tenant_id=account.tenant_id
        )
        for message in messages:
            message_id = _text(message.get("id"))
            sent_at = _timestamp(message.get("timestamp"))
            if message_id is None or sent_at is None:
                tally.skipped += 1
                tally.reasons["malformed"] += 1
                continue
            message_type = _text(message.get("type")) or "unknown"
            from_business = message.get("to") is not None or (
                bool(business) and _digits(_text(message.get("from"))) == business
            )
            status_word = _text(_mapping(message.get("history_context")).get("status"))
            if status_word is not None and status_word not in HISTORY_STATUSES:
                tally.reasons["unknown_status"] += 1
            projected = await projection.project_imported(
                connection=connection,
                sender=sender,
                message_id=message_id,
                kind=MESSAGE_KINDS.get(message_type, MessageKind.UNSUPPORTED),
                body=message_text(message, message_type),
                sent_at=sent_at,
                from_business=from_business,
                status=HISTORY_STATUSES.get(status_word or ""),
            )
            if projected.outcome is ProjectionOutcome.STORED:
                tally.imported += 1
            else:
                tally.duplicates += 1

    # ----------------------------------------------------------------- contacts

    async def _contacts(
        self,
        account: WhatsAppAccount,
        connection: ChannelConnection,
        value: Mapping[str, Any],
        tally: ProjectionTally,
    ) -> bool:
        """Apply what can be applied; whether the event is settled (CT-02 .. CT-06)."""
        waiting = False
        identities = ContactIdentityRepository(self._session, tenant_id=account.tenant_id)
        for raw in _sequence(value.get("state_sync")):
            entry = _mapping(raw)
            if entry.get("type") != "contact":
                continue
            action = entry.get("action")
            if action != "add":
                # A remove removes nothing of Wasla's (CT-04).
                tally.reasons[f"contact_{action}"] += 1
                continue
            contact_data = _mapping(entry.get("contact"))
            identifiers: list[Identifier] = []
            phone = _text(contact_data.get("phone_number"), MAX_PHONE_LENGTH)
            if phone is not None and _PHONE_LIKE.match(phone):
                identifiers.append(Identifier(kind=IdentityKind.PHONE, value=phone.lstrip("+")))
            user_id = _text(contact_data.get("user_id"))
            if user_id is not None:
                identifiers.append(Identifier(kind=IdentityKind.BSUID, value=user_id))
            if not identifiers:
                continue
            scopes = {
                identifier: await self._adapter.identity_scope(
                    self._session, connection, identifier
                )
                for identifier in identifiers
            }
            known = False
            for identifier in identifiers:
                scope = scopes[identifier]
                if await identities.find(
                    channel=Channel.WHATSAPP,
                    kind=identifier.kind,
                    scope=scope.scope,
                    scope_ref=scope.scope_ref,
                    value=identifier.value,
                ):
                    known = True
                    break
            if not known:
                # Never a contact from an address book alone (CT-03, OQ-4).
                waiting = True
                tally.contacts_waiting += 1
                continue
            resolved = await ContactIdentityService(
                session=self._session, tenant_id=account.tenant_id
            ).resolve_sender(
                connection=connection,
                identifiers=identifiers,
                scopes=scopes,
                participant_preference=self._adapter.participant_preference,
                anchor_preference=self._adapter.anchor_preference,
                profile_name=None,
                seen_at=self._now,
            )
            if resolved.conflict:
                # An identity is never moved between contacts by this field (CT-06).
                tally.reasons["conflict"] += 1
            name = _text(contact_data.get("full_name"), 200) or _text(
                contact_data.get("first_name"), 200
            )
            contact = await ContactRepository(
                self._session, tenant_id=account.tenant_id
            ).require_by_id(resolved.contact.id)
            if name is not None and contact.name is None and contact.display_name is None:
                # A member's name and Meta's profile name both win (CT-05).
                contact.display_name = name
            tally.contacts_applied += 1
        if not waiting:
            return True
        onboarded = account.onboarded_at or self._now
        history_over = (
            account.history_completed_at is not None or account.history_declined_at is not None
        )
        return history_over or self._now - onboarded > CONTACTS_WAIT


# ------------------------------------------------------------ the two syncs


@dataclass
class SyncTally:
    requested: Counter[str] = field(default_factory=Counter)


async def request_due_syncs(
    session: AsyncSession,
    *,
    client: OnboardingClient,
    credentials: CredentialService,
    now: datetime | None = None,
    limit: int = 20,
) -> SyncTally:
    """Request each Coexistence number's contacts then history sync, once, within 24 hours (SY)."""
    moment = now or datetime.now(UTC)
    tally = SyncTally()
    accounts = (
        (
            await session.execute(
                select(WhatsAppAccount)
                .where(
                    WhatsAppAccount.onboarding_mode == OnboardingMode.COEXISTENCE.value,
                    WhatsAppAccount.released_at.is_(None),
                    WhatsAppAccount.status == WhatsAppAccountStatus.ACTIVE,
                    or_(
                        WhatsAppAccount.contacts_sync_request_id.is_(None),
                        WhatsAppAccount.history_sync_request_id.is_(None),
                    ),
                    WhatsAppAccount.sync_next_attempt_at.is_not(None),
                    WhatsAppAccount.sync_next_attempt_at <= moment,
                )
                .order_by(WhatsAppAccount.sync_next_attempt_at, WhatsAppAccount.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for account in accounts:
        if account.sync_deadline_at is None or moment >= account.sync_deadline_at:
            # Never after Meta's deadline (SY-04): the step is expired.
            account.sync_next_attempt_at = None
            tally.requested["expired"] += 1
            announce_onboarding(session, account)
            continue
        try:
            token = credentials.resolve(account)
        except Exception:
            account.sync_next_attempt_at = None
            tally.requested["no_credential"] += 1
            continue
        if not token.is_own:
            account.sync_next_attempt_at = None
            tally.requested["no_credential"] += 1
            continue
        failed = False
        # Contacts first, then history (SY-05); a request accepted is never
        # sent again (SY-03).
        for sync_type in (SYNC_CONTACTS, SYNC_HISTORY):
            if _request_id(account, sync_type) is not None:
                continue
            try:
                request_id = await client.request_sync(
                    token=token.token, phone_number_id=account.phone_number_id, sync_type=sync_type
                )
            except OnboardingStepError:
                failed = True
                _bump(account, sync_type)
                tally.requested[f"{sync_type}:failure"] += 1
                continue
            _record(account, sync_type, request_id, moment)
            tally.requested[f"{sync_type}:success"] += 1
        if failed:
            attempts = max(account.contacts_sync_attempts, account.history_sync_attempts)
            delay = min(SYNC_BACKOFF_START * (2 ** max(attempts - 1, 0)), SYNC_BACKOFF_MAX)
            account.sync_next_attempt_at = moment + delay
        else:
            account.sync_next_attempt_at = None
        announce_onboarding(session, account)
    if tally.requested:
        logger.info(
            "whatsapp.coexistence_sync",
            extra={
                "event": "whatsapp.coexistence_sync",
                "es_sync_requested_total": dict(tally.requested),
            },
        )
    return tally


def _request_id(account: WhatsAppAccount, sync_type: str) -> str | None:
    if sync_type == SYNC_CONTACTS:
        return account.contacts_sync_request_id
    return account.history_sync_request_id


def _record(account: WhatsAppAccount, sync_type: str, request_id: str, at: datetime) -> None:
    if sync_type == SYNC_CONTACTS:
        account.contacts_sync_request_id = request_id
        account.contacts_sync_requested_at = at
    else:
        account.history_sync_request_id = request_id
        account.history_sync_requested_at = at


def _bump(account: WhatsAppAccount, sync_type: str) -> None:
    if sync_type == SYNC_CONTACTS:
        account.contacts_sync_attempts += 1
    else:
        account.history_sync_attempts += 1


__all__ = [
    "DISCONNECTING_EVENTS",
    "HISTORY_DECLINED_CODE",
    "CoexistenceProjector",
    "ProjectionTally",
    "SyncTally",
    "apply_account_updates",
    "request_due_syncs",
    "store_coexistence_deliveries",
]
