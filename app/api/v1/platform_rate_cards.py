"""Meta's WhatsApp rate cards, entered by platform staff (COST-01, ADR-150).

PLATFORM_STAFF only (`PLATFORM_OWNER` or `PLATFORM_ADMIN`); every workspace role
is 403. Uploading stores a card inactive and answers with its preview;
activating makes it the one campaign estimates use. Writes are audited
(`rate_card_uploaded`, `rate_card_activated`); reads are recorded like every
platform read (`platform_billing_read`, resource `billing.rate_cards`) - a rate
card is nobody's private data, but one rule for every platform read is simpler
to keep than an exception. Unlimited like the rest of the platform plane.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile, status

from app.api.dependencies import PlatformAccessAuditDep, PlatformStaffDep
from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep
from app.schemas.rate_card import RateCardDetailRead, RateCardRead
from app.services.rate_card_service import MAX_RATE_CARD_BYTES, RateCardService

router = APIRouter(
    route_class=CommittingRoute, prefix="/platform/whatsapp-rate-cards", tags=["platform"]
)


@router.post(
    "",
    response_model=RateCardDetailRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload Meta's rate card (inactive until activated)",
)
async def upload_rate_card(
    staff: PlatformStaffDep,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="Meta's rate card saved as CSV (UTF-8).")],
    # Query parameters, not form fields: the body is the file alone, as the
    # other uploads' are, so no free-form text rides in a multipart body.
    currency: Annotated[str, Query(pattern=r"^[A-Za-z]{3}$")],
    effective_from: Annotated[date, Query()],
    market_map: Annotated[
        UploadFile | None,
        File(description="Optional `calling_code,market` CSV; Meta's published map otherwise."),
    ] = None,
) -> RateCardDetailRead:
    """Parse strictly (422 `rate_card_invalid` with the line), store, and preview.

    The preview lists every rate and how each differs from the active card.
    Nothing is priced with it until `POST .../{id}/activate`.
    """
    # One byte past the cap is enough for the parser to refuse it, and no more
    # is ever held.
    content = await file.read(MAX_RATE_CARD_BYTES + 1)
    mapping = await market_map.read(MAX_RATE_CARD_BYTES + 1) if market_map else None
    view = await RateCardService(session).upload(
        content=content,
        filename=file.filename,
        currency=currency,
        effective_from=effective_from,
        market_map=mapping,
        actor=staff.user,
    )
    return RateCardDetailRead.from_view(view)


@router.post(
    "/{card_id}/activate", response_model=RateCardDetailRead, summary="Make a card the active one"
)
async def activate_rate_card(
    card_id: uuid.UUID, staff: PlatformStaffDep, session: SessionDep
) -> RateCardDetailRead:
    return RateCardDetailRead.from_view(
        await RateCardService(session).activate(card_id, actor=staff.user)
    )


@router.get("", response_model=list[RateCardRead], summary="Every uploaded rate card")
async def list_rate_cards(
    staff: PlatformStaffDep, session: SessionDep, access: PlatformAccessAuditDep
) -> list[RateCardRead]:
    cards = await RateCardService(session).list()
    access.billing_read(actor=staff.user, resource="rate_cards", returned=len(cards))
    return [RateCardRead.from_model(card) for card in cards]


@router.get("/{card_id}", response_model=RateCardDetailRead, summary="One rate card")
async def get_rate_card(
    card_id: uuid.UUID,
    staff: PlatformStaffDep,
    session: SessionDep,
    access: PlatformAccessAuditDep,
) -> RateCardDetailRead:
    view = await RateCardService(session).get(card_id)
    access.billing_read(actor=staff.user, resource="rate_card")
    return RateCardDetailRead.from_view(view)
