"""Meta's rate cards, uploaded and activated by platform staff (COST-01, ADR-150).

**The file.** Meta publishes its rate card per currency ("Rates (CSV)" on the
pricing page - in practice an Excel workbook, which staff save as CSV). The
parser is strict: rows before the one whose first cell is `Market` are Meta's
notes and are skipped; after it, every row is a market with its currency and a
rate per category, or blank. A rate is an exact decimal or `n/a`; anything else
is refused with its line number - never guessed, never a float.

**Nothing applies until it is activated.** An upload returns a preview - the
rows, and how they differ from the active card - and the card waits. The
active card is the one activated most recently.

**Markets.** A card carries its own calling-code map: the one uploaded with it,
or Meta's as Wasla transcribed it (`pricing_markets`). The card must price the
"Other" market, which every unmapped calling code falls back to.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.rate_card import (
    MAX_MARKET_LENGTH,
    PricingCategory,
    WhatsAppRateCard,
    WhatsAppRateCardMarket,
    WhatsAppRateCardRate,
)
from app.db.models.user import User
from app.services.audit_service import AuditTrail
from app.services.pricing_markets import OTHER_MARKET, default_market_map

logger = get_logger(__name__)

RATE_CARD_INVALID_CODE: Final = "rate_card_invalid"
MAX_RATE_CARD_BYTES: Final = 1_000_000
MAX_RATE_CARD_ROWS: Final = 500
#: Meta's column headings -> the closed category vocabulary. Other columns
#: (Meta's "Meta Business Agent" token price) are not message prices and are
#: ignored.
COLUMNS: Final[dict[str, PricingCategory]] = {
    "marketing": PricingCategory.MARKETING,
    "utility": PricingCategory.UTILITY,
    "authentication": PricingCategory.AUTHENTICATION,
    "authentication-international": PricingCategory.AUTHENTICATION_INTERNATIONAL,
    "service": PricingCategory.SERVICE,
}


@dataclass(frozen=True, slots=True)
class ParsedRate:
    market: str
    category: PricingCategory
    rate: Decimal


@dataclass(frozen=True, slots=True)
class RateChange:
    market: str
    category: str
    old: Decimal | None
    new: Decimal | None


@dataclass(frozen=True, slots=True)
class RateCardView:
    card: WhatsAppRateCard
    rates: list[WhatsAppRateCardRate]
    markets: int
    changes: list[RateChange]


def _refuse(line: int, message: str) -> ValidationError:
    return ValidationError(
        f"Line {line}: {message}", error_code=RATE_CARD_INVALID_CODE, details={"line": line}
    )


def _heading(cell: str) -> str:
    """A column heading as one token, whatever line ending the file was saved with.

    Meta's "Authentication-<line break>International" is `authentication-international`
    with a `\n` or a `\r\n` inside the quoted cell.
    """
    return "-".join(re.sub(r"\s*-\s*", "-", cell.strip().lower()).split())


def parse_rate_card(content: bytes, *, currency: str) -> list[ParsedRate]:
    """Meta's rate card as rows, strictly; 422 `rate_card_invalid` with the line otherwise."""
    if len(content) > MAX_RATE_CARD_BYTES:
        raise _refuse(0, "the file is larger than 1 MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _refuse(0, "the file is not UTF-8 CSV (save the workbook as CSV UTF-8).") from error
    reader = csv.reader(io.StringIO(text))
    header: dict[int, PricingCategory] | None = None
    currency_column = -1
    parsed: list[ParsedRate] = []
    seen: set[tuple[str, PricingCategory]] = set()
    for line, row in enumerate(reader, start=1):
        cells = [cell.strip() for cell in row]
        if header is None:
            if cells and cells[0].lower() == "market":
                header = {
                    index: COLUMNS[_heading(cell)]
                    for index, cell in enumerate(cells)
                    if _heading(cell) in COLUMNS
                }
                currency_column = next(
                    (i for i, cell in enumerate(cells) if cell.lower() == "currency"), -1
                )
                if PricingCategory.MARKETING not in header.values() or currency_column < 0:
                    raise _refuse(line, "the header needs Market, Currency and Marketing columns.")
            continue
        if not any(cells):
            continue
        market = cells[0]
        if not market or len(market) > MAX_MARKET_LENGTH:
            raise _refuse(line, "a row needs its market.")
        if currency_column >= len(cells) or cells[currency_column].upper() != currency:
            raise _refuse(line, f"the currency is not {currency}.")
        for index, category in header.items():
            raw = cells[index] if index < len(cells) else ""
            if raw.lower() in ("", "n/a"):
                continue
            try:
                rate = Decimal(raw.replace("$", ""))
            except InvalidOperation as error:
                raise _refuse(line, f"'{raw}' is not a rate.") from error
            if not rate.is_finite() or rate < 0:
                raise _refuse(line, f"'{raw}' is not a rate.")
            if (market, category) in seen:
                raise _refuse(line, f"{market} appears twice.")
            seen.add((market, category))
            parsed.append(ParsedRate(market=market, category=category, rate=rate))
        if len(parsed) > MAX_RATE_CARD_ROWS * len(PricingCategory):
            raise _refuse(line, "the card has too many rows.")
    if header is None:
        raise _refuse(0, "no header row starting with 'Market' was found.")
    if not parsed:
        raise _refuse(0, "the card has no rates.")
    return parsed


def parse_market_map(content: bytes) -> dict[str, str]:
    """A card's own calling-code map: `calling_code,market` rows, a header optional."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _refuse(0, "the market map is not UTF-8 CSV.") from error
    mapping: dict[str, str] = {}
    for line, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        cells = [cell.strip() for cell in row]
        if not any(cells) or (line == 1 and cells[0].lower() == "calling_code"):
            continue
        if len(cells) < 2 or not cells[0].lstrip("+").isdigit() or not cells[1]:
            raise _refuse(line, "a map row is `calling_code,market`.")
        code = cells[0].lstrip("+")
        if len(code) > 6 or code in mapping:
            raise _refuse(line, f"calling code {code} is not valid or appears twice.")
        mapping[code] = cells[1]
    return mapping


class RateCardService:
    """Upload, preview, activate and read rate cards; and find the active one."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditTrail(session)

    async def active(self) -> WhatsAppRateCard | None:
        """The card activated most recently, or None."""
        card: WhatsAppRateCard | None = await self._session.scalar(
            select(WhatsAppRateCard)
            .where(WhatsAppRateCard.activated_at.is_not(None))
            .order_by(WhatsAppRateCard.activated_at.desc(), WhatsAppRateCard.id.desc())
            .limit(1)
        )
        return card

    async def rates_of(self, card_id: uuid.UUID) -> list[WhatsAppRateCardRate]:
        return list(
            (
                await self._session.execute(
                    select(WhatsAppRateCardRate)
                    .where(WhatsAppRateCardRate.rate_card_id == card_id)
                    .order_by(WhatsAppRateCardRate.market, WhatsAppRateCardRate.category)
                )
            ).scalars()
        )

    async def market_map_of(self, card_id: uuid.UUID) -> dict[str, str]:
        rows = await self._session.execute(
            select(WhatsAppRateCardMarket.calling_code, WhatsAppRateCardMarket.market).where(
                WhatsAppRateCardMarket.rate_card_id == card_id
            )
        )
        return dict(rows.tuples().all())

    async def list(self) -> list[WhatsAppRateCard]:
        return list(
            (
                await self._session.execute(
                    select(WhatsAppRateCard).order_by(
                        WhatsAppRateCard.uploaded_at.desc(), WhatsAppRateCard.id.desc()
                    )
                )
            ).scalars()
        )

    async def get(self, card_id: uuid.UUID) -> RateCardView:
        card = await self._session.get(WhatsAppRateCard, card_id)
        if card is None:
            raise NotFoundError("No rate card matches that identifier.")
        return await self._view(card)

    async def upload(
        self,
        *,
        content: bytes,
        filename: str | None,
        currency: str,
        effective_from: date,
        market_map: bytes | None,
        actor: User,
    ) -> RateCardView:
        """Store a card, inactive, and answer with its preview."""
        currency = currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError(
                "The currency is a three-letter code.", error_code=RATE_CARD_INVALID_CODE
            )
        rates = parse_rate_card(content, currency=currency)
        mapping = parse_market_map(market_map) if market_map is not None else default_market_map()
        markets = {rate.market for rate in rates}
        if OTHER_MARKET not in markets:
            raise ValidationError(
                f"The card must price the '{OTHER_MARKET}' market: every unlisted calling code "
                "is priced there.",
                error_code=RATE_CARD_INVALID_CODE,
            )
        unpriced = sorted({market for market in mapping.values() if market not in markets})
        if unpriced:
            raise ValidationError(
                "The market map names markets the card does not price: " + ", ".join(unpriced),
                error_code=RATE_CARD_INVALID_CODE,
            )
        card = WhatsAppRateCard(
            currency=currency,
            effective_from=effective_from,
            source_filename=(filename or "")[:255] or None,
            sha256=hashlib.sha256(content).hexdigest(),
            uploaded_by_id=actor.id,
        )
        self._session.add(card)
        await self._session.flush()
        self._session.add_all(
            WhatsAppRateCardRate(
                rate_card_id=card.id,
                market=rate.market,
                category=rate.category.value,
                rate=rate.rate,
            )
            for rate in rates
        )
        self._session.add_all(
            WhatsAppRateCardMarket(rate_card_id=card.id, calling_code=code, market=market)
            for code, market in mapping.items()
        )
        await self._session.flush()
        await self._session.refresh(card, ["uploaded_at"])
        self._audit.record(
            AuditAction.RATE_CARD_UPLOADED,
            actor=actor,
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            target_type="whatsapp_rate_card",
            target_id=card.id,
            target_label=f"{currency} from {effective_from.isoformat()}",
            meta={"rates": len(rates), "markets": len(markets), "sha256": card.sha256},
        )
        logger.info("rate_card.uploaded", extra={"rate_card_id": str(card.id)})
        return await self._view(card)

    async def activate(self, card_id: uuid.UUID, *, actor: User) -> RateCardView:
        card = await self._session.get(WhatsAppRateCard, card_id, with_for_update=True)
        if card is None:
            raise NotFoundError("No rate card matches that identifier.")
        previous = await self.active()
        card.activated_at = datetime.now(UTC)
        await self._session.flush()
        self._audit.record(
            AuditAction.RATE_CARD_ACTIVATED,
            actor=actor,
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            target_type="whatsapp_rate_card",
            target_id=card.id,
            target_label=f"{card.currency} from {card.effective_from.isoformat()}",
            meta={"previous": str(previous.id) if previous is not None else None},
        )
        return await self._view(card)

    async def _view(self, card: WhatsAppRateCard) -> RateCardView:
        rates = await self.rates_of(card.id)
        mapping = await self.market_map_of(card.id)
        active = await self.active()
        changes: list[RateChange] = []
        if active is not None and active.id != card.id:
            before = {(r.market, r.category): r.rate for r in await self.rates_of(active.id)}
            after = {(r.market, r.category): r.rate for r in rates}
            for key in sorted(before.keys() | after.keys()):
                if before.get(key) != after.get(key):
                    changes.append(RateChange(key[0], key[1], before.get(key), after.get(key)))
        return RateCardView(
            card=card, rates=rates, markets=len(set(mapping.values())), changes=changes
        )


__all__ = [
    "RATE_CARD_INVALID_CODE",
    "ParsedRate",
    "RateCardService",
    "RateCardView",
    "parse_market_map",
    "parse_rate_card",
]
