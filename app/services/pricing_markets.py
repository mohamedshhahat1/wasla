"""Which rate-card market a recipient's country calling code is priced in (COST-01).

Transcribed from Meta's pricing page, section "Pricing regions on the rate
card (country calling codes)"
(https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing,
read 2026-10-16, for the rate card effective 2026-10-01). A code constant
rather than migration data: it is copied onto each rate card when the card is
uploaded without its own map, so a later edit of this table never changes the
pricing of a card already uploaded - and a card's own map, uploaded with it,
replaces it entirely.

Two calling codes are shared, and Meta's page says how only for one:

- **+1**: Canada and the United States are "North America"; the Dominican
  Republic (809, 829, 849), Jamaica (658, 876) and Puerto Rico (787, 939) are
  "Rest of Latin America" - mapped here by their four-digit prefixes.
- **+7**: Russia and Kazakhstan are both standalone markets and the page does
  not say how +7 numbers are split between them. Mapped to **Russia**, the
  higher marketing rate on the 2026-10-01 card (0.0802 against 0.0604): an
  estimate is an upper bound, and the conservative error is the larger one.
  Recorded as not documented (CAMPAIGNS_PAGE_FEATURES_IMPLEMENTATION.md).

A calling code not listed is priced at Meta's "Other" market. The longest
matching prefix wins.
"""

from __future__ import annotations

from typing import Final

SOURCE_URL: Final = (
    "https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing"
)
SOURCE_READ_ON: Final = "2026-10-16"
OTHER_MARKET: Final = "Other"

_STANDALONE: Final[dict[str, str]] = {
    "54": "Argentina",
    "880": "Bangladesh",
    "55": "Brazil",
    "56": "Chile",
    "57": "Colombia",
    "20": "Egypt",
    "33": "France",
    "49": "Germany",
    "852": "Hong Kong",
    "36": "Hungary",
    "91": "India",
    "62": "Indonesia",
    "972": "Israel",
    "964": "Iraq",
    "39": "Italy",
    "965": "Kuwait",
    "60": "Malaysia",
    "52": "Mexico",
    "212": "Morocco",
    "977": "Nepal",
    "31": "Netherlands",
    "234": "Nigeria",
    "968": "Oman",
    "92": "Pakistan",
    "51": "Peru",
    "48": "Poland",
    "974": "Qatar",
    "40": "Romania",
    # +7 is Russia and Kazakhstan; see the module docstring.
    "7": "Russia",
    "966": "Saudi Arabia",
    "65": "Singapore",
    "94": "Sri Lanka",
    "27": "South Africa",
    "34": "Spain",
    "90": "Turkey",
    "380": "Ukraine",
    "971": "United Arab Emirates",
    "44": "United Kingdom",
}

_REGIONS: Final[dict[str, tuple[str, ...]]] = {
    "North America": ("1",),
    "Rest of Africa": (
        "213", "244", "229", "267", "226", "257", "237", "235", "242", "291",
        "251", "241", "220", "233", "245", "225", "254", "266", "231", "218",
        "261", "265", "223", "222", "258", "264", "227", "250", "221", "232",
        "252", "211", "249", "268", "255", "228", "216", "256", "260", "263",
    ),
    "Rest of Asia Pacific": (
        "93", "61", "855", "86", "81", "856", "976", "64", "675", "63", "886",
        "992", "66", "993", "998", "84",
    ),
    "Rest of Central & Eastern Europe": (
        "355", "374", "994", "375", "359", "385", "420", "995", "30", "371",
        "370", "373", "389", "381", "421", "386",
    ),
    "Rest of Western Europe": ("43", "32", "45", "358", "353", "47", "351", "46", "41"),
    "Rest of Latin America": (
        "591", "506", "1809", "1829", "1849", "593", "503", "502", "509", "504",
        "1658", "1876", "505", "507", "595", "1787", "1939", "598", "58",
    ),
    "Rest of Middle East": ("973", "962", "961", "967"),
}  # fmt: skip


def default_market_map() -> dict[str, str]:
    """Calling code -> market, as Meta's page lists them."""
    mapping = dict(_STANDALONE)
    for market, codes in _REGIONS.items():
        for code in codes:
            mapping[code] = market
    return mapping


def market_for(number: str, mapping: dict[str, str]) -> str:
    """The market a WhatsApp number is priced in: its longest listed calling code, else Other."""
    digits = number.lstrip("+")
    for length in range(min(6, len(digits)), 0, -1):
        found = mapping.get(digits[:length])
        if found is not None:
            return found
    return OTHER_MARKET


__all__ = [
    "OTHER_MARKET",
    "SOURCE_READ_ON",
    "SOURCE_URL",
    "default_market_map",
    "market_for",
]
