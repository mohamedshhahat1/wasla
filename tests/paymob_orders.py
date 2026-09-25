"""Deterministic Paymob order ids for test doubles.

Real Paymob answers every Create Intention with an `intention_order_id`, and
every transaction callback about that intention carries the same number as
`order.id` inside its HMAC. Wasla binds settlement to that equality (BILL-11)
and finds a saved card's workspace by it (BILL-04).

A test double has to reproduce the pairing or it tests nothing - which is how
the audit found the old suite passing while real TOKEN callbacks never matched
(structural gap 1). So both halves derive the order from the one value both
sides know: our own reference, the payment id sent as `special_reference`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

# Test and live Paymob integrations used by the doubles: a customer-facing card
# integration and the MOTO integration automatic renewals run on.
CARD_INTEGRATION_ID = 4097558
MOTO_INTEGRATION_ID = 5934829


def order_for(reference: object) -> int:
    """The Paymob order id a double assigns to the intention for `reference`."""
    if reference is None:
        return 1
    try:
        value = uuid.UUID(str(reference))
    except ValueError:
        return 100_000_000 + (hash(str(reference)) % 900_000_000)
    return 100_000_000 + value.int % 900_000_000


def order_from_request(request: httpx.Request) -> int:
    """The order for an intention request, read from its `special_reference`."""
    try:
        body: dict[str, Any] = json.loads(request.content or b"{}")
    except ValueError:
        return 1
    return order_for(body.get("special_reference"))
