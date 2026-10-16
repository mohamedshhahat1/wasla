"""Each person's own values in a campaign's template (PER-01 .. PER-05, ADR-148).

**Where a value may come from - and nothing else.** The contact's display name,
the active lead's name, interest, status (as its label) and budget (amount and
currency), one of the workspace's own lead custom fields, or a constant. The
phone number is not a source: a template is not where a business repeats a
customer's number back to them. A URL button's suffix and a copy code stay
campaign constants: a personal value in a link leaves Wasla in a URL.

**Written down when the audience is.** Every recipient's values are resolved
once, at materialisation, and stored on the recipient row (`rendered_parameters`)
with the contact they were resolved for - the same principle as the audience
itself: written down, not computed. The send uses exactly those; renaming a
contact afterwards does not change what this campaign sends.

**A fallback for every person-dependent value.** Missing, null or empty after
normalisation - or longer than `CAMPAIGN_PARAMETER_MAX_CHARS` - is the
fallback. Normalisation (PER-05): newline, carriage return and tab become a
space, runs of spaces become one, control characters go, and the result is
trimmed. Meta documents no rule of its own for parameter values on the
components or error-code pages (read 2026-10-16), so this one applies. A
person whose values would carry the template body past Meta's 1,024
characters is sent the fallbacks instead, and the row records it.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from app.db.models.lead import LeadStatus
from app.db.models.whatsapp_template import PLACEHOLDER

#: Meta's limit on a template body ("Template components", read 2026-10-16).
META_BODY_MAX_CHARS: Final = 1024
#: A custom field key a mapping may name.
MAX_CUSTOM_FIELD_KEY_LENGTH: Final = 100
_SPACES: Final = re.compile(r"[ \t\r\n]+")


class ParameterSource(StrEnum):
    """Where a body or text-header value comes from. Closed (PER-01)."""

    CONSTANT = "constant"
    CONTACT_DISPLAY_NAME = "contact.display_name"
    LEAD_NAME = "lead.name"
    LEAD_INTEREST = "lead.interest"
    LEAD_STATUS = "lead.status"
    LEAD_BUDGET = "lead.budget"
    LEAD_CUSTOM_FIELD = "lead.custom_fields"


#: How a lead status reads in a message.
STATUS_LABELS: Final[dict[str, str]] = {
    LeadStatus.NEW.value: "New",
    LeadStatus.CONTACTED.value: "Contacted",
    LeadStatus.QUALIFIED.value: "Qualified",
    LeadStatus.PROPOSAL.value: "Proposal",
    LeadStatus.WON.value: "Won",
    LeadStatus.LOST.value: "Lost",
}


@dataclass(frozen=True, slots=True)
class Mapping:
    """One placeholder's value: a constant, or a person's field with its fallback."""

    source: ParameterSource
    #: The constant's text, for `constant`.
    value: str | None = None
    #: The custom field's key, for `lead.custom_fields`.
    key: str | None = None
    #: What a person without the field is sent. Required unless constant.
    fallback: str | None = None

    @property
    def personal(self) -> bool:
        return self.source is not ParameterSource.CONSTANT

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "value": self.value,
            "key": self.key,
            "fallback": self.fallback,
        }

    @classmethod
    def from_dict(cls, stored: dict[str, Any]) -> Mapping:
        return cls(
            source=ParameterSource(stored["source"]),
            value=stored.get("value"),
            key=stored.get("key"),
            fallback=stored.get("fallback"),
        )

    def default(self) -> str:
        """What is sent when there is no person to resolve for: the constant or the fallback."""
        return (self.value if self.source is ParameterSource.CONSTANT else self.fallback) or ""


def normalise(value: Any) -> str:
    """PER-05: one line, single spaces, no control characters, trimmed."""
    if value is None:
        return ""
    text = str(value)
    text = _SPACES.sub(" ", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Cc")
    return text.strip()


@dataclass(frozen=True, slots=True)
class PersonFacts:
    """What a contact and their active lead say, for resolving values."""

    contact_id: uuid.UUID
    display_name: str | None = None
    lead_name: str | None = None
    lead_interest: str | None = None
    lead_status: str | None = None
    lead_budget_amount: Decimal | None = None
    lead_budget_currency: str | None = None
    lead_custom_fields: dict[str, Any] | None = None


def _budget(amount: Decimal | None, currency: str | None) -> str | None:
    if amount is None:
        return None
    whole = amount.quantize(Decimal(1)) if amount == amount.to_integral_value() else amount
    text = f"{whole:,}"
    return f"{text} {currency}" if currency else text


def raw_value(mapping: Mapping, facts: PersonFacts) -> Any:
    source = mapping.source
    if source is ParameterSource.CONSTANT:
        return mapping.value
    if source is ParameterSource.CONTACT_DISPLAY_NAME:
        return facts.display_name
    if source is ParameterSource.LEAD_NAME:
        return facts.lead_name
    if source is ParameterSource.LEAD_INTEREST:
        return facts.lead_interest
    if source is ParameterSource.LEAD_STATUS:
        return STATUS_LABELS.get(facts.lead_status or "")
    if source is ParameterSource.LEAD_BUDGET:
        return _budget(facts.lead_budget_amount, facts.lead_budget_currency)
    fields = facts.lead_custom_fields or {}
    found = fields.get(mapping.key or "")
    return found if isinstance(found, str | int | float | Decimal) else None


@dataclass(frozen=True, slots=True)
class Rendered:
    """One person's values, as stored on the recipient row."""

    contact_id: uuid.UUID
    body: tuple[str, ...]
    header: tuple[str, ...]
    body_fallbacks: tuple[int, ...]
    header_fallbacks: tuple[int, ...]
    #: Their values would have carried the body past Meta's limit: all fallbacks.
    body_too_long: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "contact_id": str(self.contact_id),
            "body": list(self.body),
            "header": list(self.header),
            "fallbacks": {"body": list(self.body_fallbacks), "header": list(self.header_fallbacks)},
            "body_too_long": self.body_too_long,
        }

    @classmethod
    def from_dict(cls, stored: dict[str, Any]) -> Rendered:
        fallbacks = stored.get("fallbacks") or {}
        return cls(
            contact_id=uuid.UUID(str(stored["contact_id"])),
            body=tuple(str(value) for value in stored.get("body") or ()),
            header=tuple(str(value) for value in stored.get("header") or ()),
            body_fallbacks=tuple(int(index) for index in fallbacks.get("body") or ()),
            header_fallbacks=tuple(int(index) for index in fallbacks.get("header") or ()),
            body_too_long=bool(stored.get("body_too_long")),
        )


def _resolve(
    mappings: tuple[Mapping, ...], facts: PersonFacts, *, max_chars: int
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    values: list[str] = []
    fell_back: list[int] = []
    for index, mapping in enumerate(mappings):
        value = normalise(raw_value(mapping, facts))
        if mapping.personal and (not value or len(value) > max_chars):
            value = normalise(mapping.fallback)
            fell_back.append(index)
        values.append(value)
    return tuple(values), tuple(fell_back)


def fill(text: str | None, names: tuple[str, ...], values: tuple[str, ...]) -> str:
    """A template text with its placeholders replaced, in the spec's order."""
    if not text:
        return ""
    lookup = dict(zip(names, values, strict=False))

    def replace(match: re.Match[str]) -> str:
        return lookup.get(match.group(1), match.group(0))

    return PLACEHOLDER.sub(replace, text)


def resolve(
    *,
    body: tuple[Mapping, ...],
    header: tuple[Mapping, ...],
    facts: PersonFacts,
    max_chars: int,
    body_text: str | None,
    body_names: tuple[str, ...],
) -> Rendered:
    """One person's values, with fallbacks where theirs are missing or too long (PER-03)."""
    body_values, body_fallbacks = _resolve(body, facts, max_chars=max_chars)
    header_values, header_fallbacks = _resolve(header, facts, max_chars=max_chars)
    too_long = len(fill(body_text, body_names, body_values)) > META_BODY_MAX_CHARS
    if too_long:
        body_values = tuple(normalise(mapping.default()) for mapping in body)
        body_fallbacks = tuple(index for index, mapping in enumerate(body) if mapping.personal)
    return Rendered(
        contact_id=facts.contact_id,
        body=body_values,
        header=header_values,
        body_fallbacks=body_fallbacks,
        header_fallbacks=header_fallbacks,
        body_too_long=too_long,
    )


__all__ = [
    "MAX_CUSTOM_FIELD_KEY_LENGTH",
    "META_BODY_MAX_CHARS",
    "STATUS_LABELS",
    "Mapping",
    "ParameterSource",
    "PersonFacts",
    "Rendered",
    "fill",
    "normalise",
    "raw_value",
    "resolve",
]
