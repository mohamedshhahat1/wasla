"""Meta's rate card parsed strictly, and calling codes priced by market (COST-01).

The fixture is Meta's USD rate card effective 2026-10-01, downloaded once from
the pricing page's "Rates (CSV)" link (business.whatsapp.com /
developers.facebook.com pricing, read 2026-10-16) - an Excel workbook,
sha256 3e996b56a86d9e7f70a9cfded896c8f39a19cb0e88683ed06b28f191856c9260,
20,545 bytes - saved as CSV with its preamble rows kept, as staff would.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.core.exceptions import ValidationError
from app.db.models.rate_card import PricingCategory
from app.services.pricing_markets import OTHER_MARKET, default_market_map, market_for
from app.services.rate_card_service import parse_market_map, parse_rate_card

FIXTURE = Path(__file__).parents[1] / "fixtures" / "whatsapp_rate_card_usd_2026-10-01.csv"


def _rates() -> dict[tuple[str, PricingCategory], Decimal]:
    parsed = parse_rate_card(FIXTURE.read_bytes(), currency="USD")
    return {(row.market, row.category): row.rate for row in parsed}


def test_meta_s_card_is_read_exactly_with_its_preamble_and_two_line_heading() -> None:
    rates = _rates()
    assert rates[("Egypt", PricingCategory.MARKETING)] == Decimal("0.0644")
    assert rates[("Egypt", PricingCategory.AUTHENTICATION_INTERNATIONAL)] == Decimal("0.065")
    assert rates[("Saudi Arabia", PricingCategory.MARKETING)] == Decimal("0.0576")
    assert rates[("Colombia", PricingCategory.UTILITY)] == Decimal("0.0008")
    assert rates[(OTHER_MARKET, PricingCategory.MARKETING)] == Decimal("0.0604")
    # `n/a` is no price, not zero.
    assert ("Argentina", PricingCategory.AUTHENTICATION_INTERNATIONAL) not in rates
    markets = {market for market, _ in rates}
    assert len(markets) == 47
    assert all(isinstance(rate, Decimal) for rate in rates.values())


@pytest.mark.parametrize(
    ("content", "line", "fragment"),
    [
        (b"Market,Currency,Marketing\nEgypt,USD,abc\n", 2, "not a rate"),
        (b"Market,Currency,Marketing\nEgypt,EUR,0.06\n", 2, "currency"),
        (b"Market,Currency,Marketing\nEgypt,USD,-0.01\n", 2, "not a rate"),
        (b"Market,Currency,Marketing\nEgypt,USD,0.06\nEgypt,USD,0.07\n", 3, "twice"),
        (b"Country,Price\nEgypt,0.06\n", 0, "header"),
        (b"Market,Marketing\nEgypt,0.06\n", 1, "Currency"),
        (b"Market,Currency,Marketing\nEgypt,USD,NaN\n", 2, "not a rate"),
    ],
)
def test_a_malformed_card_is_refused_with_its_line(
    content: bytes, line: int, fragment: str
) -> None:
    with pytest.raises(ValidationError) as refused:
        parse_rate_card(content, currency="USD")
    assert refused.value.error_code == "rate_card_invalid"
    assert (refused.value.details or {}).get("line") == line
    assert fragment in str(refused.value)


@pytest.mark.parametrize(
    ("number", "market"),
    [
        ("201001234567", "Egypt"),
        ("+966501234567", "Saudi Arabia"),
        ("971501234567", "United Arab Emirates"),
        ("962791234567", "Rest of Middle East"),
        ("15551234567", "North America"),
        ("18095551234", "Rest of Latin America"),
        ("18765551234", "Rest of Latin America"),
        ("79161234567", "Russia"),
        ("2131234567", "Rest of Africa"),
        ("999123456", OTHER_MARKET),
    ],
)
def test_the_longest_calling_code_names_the_market(number: str, market: str) -> None:
    assert market_for(number, default_market_map()) == market


def test_every_mapped_market_is_priced_on_meta_s_card() -> None:
    markets = {market for market, _ in _rates()}
    assert set(default_market_map().values()) <= markets


def test_a_companion_map_is_codes_and_markets() -> None:
    assert parse_market_map(b"calling_code,market\n+20,Egypt\n966,Saudi Arabia\n") == {
        "20": "Egypt",
        "966": "Saudi Arabia",
    }
    with pytest.raises(ValidationError):
        parse_market_map(b"twenty,Egypt\n")
    with pytest.raises(ValidationError):
        parse_market_map(b"20,Egypt\n20,Other\n")


def test_prices_multiply_exactly() -> None:
    rate = _rates()[("Egypt", PricingCategory.MARKETING)]
    assert rate * 3 == Decimal("0.1932")
    assert sum((rate for _ in range(10)), Decimal(0)) == Decimal("0.6440")
