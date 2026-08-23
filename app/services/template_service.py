"""Keeping the local template registry in step with Meta's.

Templates are drafted and approved in the WhatsApp Business Manager, not here.
This service reads what Meta says and writes it down, so that the two places
that must not send an unapproved template — a follow-up leaving the service
window and a campaign about to write to thousands of people — can ask a table
instead of a third party.

Syncing is an explicit administrative action rather than a background job. A
workspace approves a template and then wants to use it, and a sync it can see
succeed or fail is more useful than one it has to wait for. The call is bounded:
a 10-second timeout, a capped page size and a capped page count.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.whatsapp_template import (
    MAX_LANGUAGE_LENGTH,
    MAX_TEMPLATE_NAME_LENGTH,
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
    count_placeholders,
)
from app.integrations.whatsapp.client import WhatsAppClient, build_http_client
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.services.credential_service import CredentialService

logger = get_logger(__name__)

# Meta's spellings, mapped onto ours. Anything absent from this table becomes
# UNKNOWN, which is not sendable: a status introduced after this was written
# must fail closed rather than be guessed at.
META_STATUSES: Final[dict[str, TemplateStatus]] = {
    "APPROVED": TemplateStatus.APPROVED,
    "PENDING": TemplateStatus.PENDING,
    "IN_APPEAL": TemplateStatus.PENDING,
    "REJECTED": TemplateStatus.REJECTED,
    "PAUSED": TemplateStatus.PAUSED,
    # Sending is refused while the limit holds, which is what PAUSED means here.
    "LIMIT_EXCEEDED": TemplateStatus.PAUSED,
    "DISABLED": TemplateStatus.DISABLED,
    "PENDING_DELETION": TemplateStatus.DISABLED,
    "DELETED": TemplateStatus.DISABLED,
}

META_CATEGORIES: Final[dict[str, TemplateCategory]] = {
    "MARKETING": TemplateCategory.MARKETING,
    "UTILITY": TemplateCategory.UTILITY,
    "AUTHENTICATION": TemplateCategory.AUTHENTICATION,
    # Meta's earlier vocabulary. Still returned for templates approved under it.
    "TRANSACTIONAL": TemplateCategory.UTILITY,
    "OTP": TemplateCategory.AUTHENTICATION,
}

MAX_REJECTION_REASON_LENGTH: Final = 200
# Recorded on a template that has vanished from the account. Kept as a row so a
# campaign that references it still resolves, and so the reason it stopped
# working is legible rather than a missing foreign key.
WITHDRAWN_REASON: Final = "No longer present in the WhatsApp Business account."


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """What one sync changed."""

    account_id: uuid.UUID
    created: int = 0
    updated: int = 0
    withdrawn: int = 0

    @property
    def seen(self) -> int:
        return self.created + self.updated


class TemplateService:
    """The template registry of one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        settings: Settings,
        whatsapp: WhatsAppClient | None = None,
    ) -> None:
        """`whatsapp` overrides how templates are fetched.

        Optional because every read here works without one, and because a test
        needs to drive a sync without a WhatsApp account. When it is absent, a
        sync builds a client for the one call and closes it again.
        """
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings
        self._whatsapp = whatsapp
        # The same resolver the send path uses, so a sync reads the registry
        # with whichever credential that number sends with (ADR-034). Reading a
        # workspace's templates through the *platform* token when the workspace
        # has its own is a wider credential than the operation needs.
        self._credentials = CredentialService(settings)
        self._templates = WhatsAppTemplateRepository(session, tenant_id=tenant_id)
        self._accounts = WhatsAppAccountRepository(session, tenant_id=tenant_id)

    # ------------------------------------------------------------------ reads

    async def get(self, template_id: uuid.UUID) -> WhatsAppTemplate:
        return await self._templates.require_by_id(template_id)

    async def list_templates(
        self,
        *,
        account_id: uuid.UUID | None = None,
        status: TemplateStatus | None = None,
        category: TemplateCategory | None = None,
        limit: int = 100,
    ) -> list[WhatsAppTemplate]:
        if account_id is not None:
            # Resolved through the scoped repository so an account id belonging
            # to another workspace answers not-found rather than an empty list.
            await self._accounts.require_by_id(account_id)
        return await self._templates.list_templates(
            account_id=account_id,
            status=status,
            category=category,
            limit=limit,
        )

    async def refusal_reason(self, *, name: str, language: str) -> str | None:
        """Why this template must not be sent, or None if nothing objects."""
        return refusal_reason_for(await self._templates.find_anywhere(name=name, language=language))

    # ------------------------------------------------------------------- sync

    async def sync(self, account_id: uuid.UUID) -> SyncOutcome:
        """Mirror one account's templates from Meta.

        Rows are matched on `(name, language)`, which is Meta's own identity for
        a template. A template that has disappeared from the account is marked
        `DISABLED` rather than deleted: a campaign may reference it, and the row
        is the only place a workspace can read why its template stopped working.
        """
        # Live claims only. Syncing a number the workspace has given up would
        # read a business account it no longer holds, and would refill a
        # registry that nothing may send from.
        account = await self._accounts.require_live_by_id(account_id)

        if self._whatsapp is not None:
            payloads = await self._whatsapp.list_templates(waba_id=account.waba_id)
        else:
            # `account.waba_id` is Meta's own answer from the ownership check,
            # not a value the workspace typed (ADR-037). That matters here more
            # than anywhere else: before proof existed, a workspace could name
            # any business account it liked and this call would read that
            # account's templates with whatever credential it ran under.
            token = self._credentials.resolve(account).token
            async with build_http_client() as http:
                client = WhatsAppClient(
                    http=http,
                    access_token=token,
                    api_version=self._settings.meta_api_version,
                )
                payloads = await client.list_templates(waba_id=account.waba_id)

        existing = {
            (row.name, row.language): row
            for row in await self._templates.list_for_account(account_id)
        }
        now = datetime.now(UTC)
        created = updated = 0
        seen: set[tuple[str, str]] = set()

        for payload in payloads:
            identity = _identity(payload)
            if identity is None:
                # A template with no name or no language is not addressable, so
                # there is nothing a send could do with it.
                logger.warning(
                    "template.unusable_payload",
                    extra={"account_id": str(account_id)},
                )
                continue

            seen.add(identity)
            row = existing.get(identity)
            if row is None:
                row = self._templates.create(
                    account_id=account_id,
                    name=identity[0],
                    language=identity[1],
                )
                created += 1
            else:
                updated += 1
            _apply(row, payload, synced_at=now)

        withdrawn = 0
        for identity, row in existing.items():
            if identity in seen or row.status is TemplateStatus.DISABLED:
                continue
            row.status = TemplateStatus.DISABLED
            row.rejection_reason = WITHDRAWN_REASON
            row.synced_at = now
            withdrawn += 1

        logger.info(
            "template.synced",
            extra={
                "account_id": str(account_id),
                # Prefixed because `created` is one of LogRecord's own
                # attributes, and passing it through `extra` raises rather than
                # being ignored. The other two follow it for symmetry.
                "templates_created": created,
                "templates_updated": updated,
                "templates_withdrawn": withdrawn,
            },
        )
        return SyncOutcome(
            account_id=account_id,
            created=created,
            updated=updated,
            withdrawn=withdrawn,
        )


def refusal_reason_for(template: WhatsAppTemplate | None) -> str | None:
    """Why a registry row forbids a send, or None if nothing objects.

    The asymmetry here is deliberate and is the whole behaviour of this
    function. A template the registry has *never heard of* is allowed through: a
    workspace that has not synced yet would otherwise have every
    template-bearing follow-up refused, and "unknown" is indistinguishable from
    "never synced". A template the registry *does* know and Meta has not
    approved is refused, because there the answer is real.

    A campaign applies a stricter rule of its own — the template must exist and
    be approved — because a campaign is a new thing a workspace sets up
    deliberately, and requiring a sync first costs it one click rather than a
    regression.

    A free function so a caller holding only the repository can apply the rule
    without constructing a service that would otherwise need settings it has no
    use for.
    """
    if template is None or template.is_sendable:
        return None
    return f"WhatsApp reports this template as {template.status.value}."


def _identity(payload: dict[str, Any]) -> tuple[str, str] | None:
    """The `(name, language)` pair a send would use, if the payload has one."""
    name = payload.get("name")
    language = payload.get("language")
    if not isinstance(name, str) or not isinstance(language, str):
        return None
    name, language = name.strip(), language.strip()
    if not name or not language:
        return None
    return name[:MAX_TEMPLATE_NAME_LENGTH], language[:MAX_LANGUAGE_LENGTH]


def _apply(row: WhatsAppTemplate, payload: dict[str, Any], *, synced_at: datetime) -> None:
    """Copy Meta's answer onto a registry row."""
    meta_id = payload.get("id")
    row.meta_template_id = str(meta_id)[:64] if meta_id is not None else None

    status = payload.get("status")
    row.status = META_STATUSES.get(
        status.strip().upper() if isinstance(status, str) else "",
        TemplateStatus.UNKNOWN,
    )
    category = payload.get("category")
    row.category = META_CATEGORIES.get(
        category.strip().upper() if isinstance(category, str) else "",
        TemplateCategory.UNKNOWN,
    )

    components = payload.get("components")
    row.components = components if isinstance(components, list) else None
    row.body_text = _body_text(components)
    row.variable_count = count_placeholders(row.body_text)

    row.quality_rating = _quality_rating(payload)
    row.rejection_reason = _rejection_reason(payload)
    row.synced_at = synced_at


def _body_text(components: Any) -> str | None:
    """The body component's text, which is the part variables belong to."""
    if not isinstance(components, list):
        return None
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("type", "")).strip().upper() != "BODY":
            continue
        text = component.get("text")
        if isinstance(text, str) and text:
            return text
    return None


def _quality_rating(payload: dict[str, Any]) -> str | None:
    """Meta reports this two ways depending on the API version."""
    score = payload.get("quality_score")
    if isinstance(score, dict):
        value = score.get("score")
        if isinstance(value, str) and value:
            return value[:16]
    rating = payload.get("quality_rating")
    return rating[:16] if isinstance(rating, str) and rating else None


def _rejection_reason(payload: dict[str, Any]) -> str | None:
    reason = payload.get("rejected_reason")
    if not isinstance(reason, str) or not reason or reason.strip().upper() == "NONE":
        return None
    return reason[:MAX_REJECTION_REASON_LENGTH]


__all__ = [
    "META_CATEGORIES",
    "META_STATUSES",
    "SyncOutcome",
    "TemplateService",
    "refusal_reason_for",
]
