"""Is every price, fee and percentage in a reply in what the turn retrieved? (RQ-19, FX-3.13)

A wrong number is the most damaging answer a sales or support agent can give,
and grounding was prompt-only. This checks, deterministically and without a
model call, the two kinds of number that matter most:

- **currency amounts** - a number next to a currency word or sign, in English
  or Arabic (`7,900 EGP`, `٧٩٠٠ جنيه`, `$15`, `15 ريال`);
- **percentages** - a number with `%`, `٪`, "percent" or "في المية".

Each is compared, after `for_matching` folds Arabic-Indic digits and the
thousands separators are removed, against every number in **what this turn
retrieved** and **what the customer said** - and in the agent's own
instructions, which is where a business writes facts like its delivery fee
(a deviation from RQ-19's two sources, recorded: without it every fee the
business configured in the prompt is a false positive). A number is grounded
when it equals one of them, or is the sum, difference or product of two - a
total for 150 m at 4,500 EGP/m is arithmetic on grounded numbers, not an
invention.

Dates, phone numbers, model numbers and quantities are not checked: they are
not prices, and checking them would flood the guard with false positives.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from itertools import combinations_with_replacement
from typing import Final

from app.services.text_normalisation import for_matching

CURRENCY: Final = "currency"
PERCENT: Final = "percent"

# The words and signs that make a number a price. Lower-case; Arabic as typed.
_CURRENCY_WORDS: Final = (
    r"egp|le|l\.e\.?|usd|eur|gbp|sar|aed|kwd|qar|dollars?|pounds?|euros?|riyals?|dirhams?|"
    "جنيه|جنيها|جنيهات|ج\\.م\\.?|ج\\.م|ريال|ريالات|درهم|دولار|يورو"
)
_CURRENCY_SIGNS: Final = r"[$€£]"
# Arabic thousands and decimal separators and percent sign, built rather than
# typed so they survive every editor.
_THOUSANDS_SIGN: Final = chr(0x066C)
_DECIMAL_SIGN: Final = chr(0x066B)
_PERCENT_SIGN: Final = chr(0x066A)
_NUMBER: Final = (
    rf"\d{{1,3}}(?:[,{_THOUSANDS_SIGN}]\d{{3}})+(?:[.{_DECIMAL_SIGN}]\d+)?"
    rf"|\d+(?:[.{_DECIMAL_SIGN}]\d+)?"
)
_AMOUNT_AFTER: Final = re.compile(
    rf"(?<![\w.])({_NUMBER})\s?(?:{_CURRENCY_SIGNS}|(?:{_CURRENCY_WORDS})(?![\w]))",
    re.IGNORECASE,
)
_AMOUNT_BEFORE: Final = re.compile(
    rf"(?:{_CURRENCY_SIGNS}|(?<![\w])(?:{_CURRENCY_WORDS}))\s?({_NUMBER})(?![\w])",
    re.IGNORECASE,
)
_PERCENT_WORDS: Final = r"percent\b|per cent\b|في المية|في المائة|بالمئة|بالمائة"
_PERCENT: Final = re.compile(
    rf"(?<![\w.])({_NUMBER})\s?(?:%|{_PERCENT_SIGN}|{_PERCENT_WORDS})",
    re.IGNORECASE,
)
_ANY_NUMBER: Final = re.compile(rf"(?<![\w])({_NUMBER})(?![\d])")
# The largest quantity a customer's own number may multiply a grounded price by.
MAX_QUANTITY: Final = Decimal(100_000)


def _value(raw: str) -> Decimal | None:
    cleaned = raw.replace(",", "").replace(_THOUSANDS_SIGN, "").replace(_DECIMAL_SIGN, ".")
    try:
        return Decimal(cleaned).normalize()
    except InvalidOperation:
        return None


@dataclass(frozen=True, slots=True)
class QuotedNumber:
    """A price or percentage found in a reply."""

    value: Decimal
    kind: str


@dataclass(frozen=True, slots=True)
class GroundingCheck:
    """What one reply quoted, and which of it the sources do not support."""

    checked: tuple[QuotedNumber, ...] = ()
    ungrounded: tuple[QuotedNumber, ...] = field(default=())

    @property
    def grounded(self) -> int:
        return len(self.checked) - len(self.ungrounded)


def quoted_numbers(reply: str) -> list[QuotedNumber]:
    """Every currency amount and percentage in a reply, once each."""
    text = for_matching(reply)
    found: dict[tuple[Decimal, str], QuotedNumber] = {}
    for pattern, kind in (
        (_AMOUNT_AFTER, CURRENCY),
        (_AMOUNT_BEFORE, CURRENCY),
        (_PERCENT, PERCENT),
    ):
        for match in pattern.finditer(text):
            value = _value(match.group(1))
            if value is not None:
                found.setdefault((value, kind), QuotedNumber(value=value, kind=kind))
    return list(found.values())


def source_numbers(texts: Iterable[str]) -> set[Decimal]:
    """Every number in the given texts, as values."""
    values: set[Decimal] = set()
    for text in texts:
        for match in _ANY_NUMBER.finditer(for_matching(text)):
            value = _value(match.group(1))
            if value is not None:
                values.add(value)
    return values


def _derivable(value: Decimal, sources: set[Decimal], quantities: set[Decimal]) -> bool:
    if value in sources:
        return True
    pool = sorted(sources)
    if len(pool) <= 60:
        for first, second in combinations_with_replacement(pool, 2):
            if value in (first + second, abs(first - second)):
                return True
    return any(
        quantity * price == value
        for quantity in quantities
        if 0 < quantity <= MAX_QUANTITY
        for price in pool
    )


def check(
    reply: str,
    *,
    retrieved: Iterable[str],
    customer: Iterable[str],
    instructions: str = "",
) -> GroundingCheck:
    """Which of the reply's prices and percentages no source supports."""
    quoted = quoted_numbers(reply)
    if not quoted:
        return GroundingCheck()
    customer_texts = list(customer)
    sources = source_numbers([*retrieved, *customer_texts, instructions])
    quantities = source_numbers(customer_texts) | source_numbers([reply])
    # To a fixed point: a total built on a figure the reply itself derived -
    # 150 m x 4,500 = 675,000, then + 50 delivery - is still arithmetic on
    # grounded numbers.
    pending = list(quoted)
    while True:
        derived = [
            number
            for number in pending
            if _derivable(number.value, sources, quantities if number.kind == CURRENCY else set())
        ]
        if not derived:
            break
        sources |= {number.value for number in derived}
        pending = [number for number in pending if number not in derived]
    return GroundingCheck(checked=tuple(quoted), ungrounded=tuple(pending))


def correction_instruction(ungrounded: Iterable[QuotedNumber]) -> str:
    """What the corrective round tells the model (RQ-19 `correct`)."""
    listed = ", ".join(format(n.value, "f") for n in ungrounded)
    return (
        f"Your reply states {listed}, which is not in the knowledge base sources "
        "or the customer's messages. Rewrite the reply using only figures the "
        "sources state, or say you do not have that figure and offer to check "
        "with a colleague. Do not call any tools."
    )


__all__ = [
    "CURRENCY",
    "PERCENT",
    "GroundingCheck",
    "QuotedNumber",
    "check",
    "correction_instruction",
    "quoted_numbers",
    "source_numbers",
]
