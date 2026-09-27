"""How the Paymob adapter reads refunds, as real Paymob Test sends them (PAY-E2E-01/03).

Every fixture here is a recorded shape, not a guess. On 2026-09-27 one 99.00 EGP
payment (transaction 542754263, order 619143122) was refunded 30.00 and then
69.00 through the real refund API, and read back through the real APIs:

* both refund **callbacks** were about the *parent*, 542754263 - `success:
  true`, `is_refunded: true`, `is_refund: false`, no parent - with
  `refunded_amount_cents` 3000 and then 9900: a running total;
* **Transaction Inquiry** by our reference answered with the latest transaction
  on the order, the *refund child* 542755547 - `success: true`, `is_refund:
  true`, `is_refunded: false`, `refunded_amount_cents: 0`, `amount_cents:
  6900`, `has_parent_transaction: true`, `parent_transaction: 542754263`;
* ``GET /api/acceptance/transactions/542754263`` answered with the parent,
  `is_refunded: true`, `refunded_amount_cents: 9900`.

The first shape used to be keyed ``542754263:refunded`` whatever the total, so
the second refund was dropped as a duplicate (PAY-E2E-01). The second used to
be read as a 69.00 collection (PAY-E2E-03).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.integrations.billing.checkout import EventKind, InquiryVerdict
from app.integrations.billing.paymob import PaymobProvider, hmac_signature

HMAC_SECRET = "refund-semantics-hmac"
PARENT = 542754263
FIRST_CHILD = 542755261
SECOND_CHILD = 542755547
ORDER = 619143122
REFERENCE = "ab29fd99-fc81-4a49-b9f4-a4b8c3dcf068"
INTEGRATION = 5885262


def parent(*, refunded_cents: int | None, is_refunded: bool = True) -> dict[str, Any]:
    """The collecting transaction, as its refund callbacks and a read of it carry it."""
    body: dict[str, Any] = {
        "id": PARENT,
        "pending": False,
        "amount_cents": 9900,
        "success": True,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": False,
        "is_refunded": is_refunded,
        "is_refund": False,
        "is_void": False,
        "is_3d_secure": True,
        "integration_id": INTEGRATION,
        "has_parent_transaction": False,
        "parent_transaction": None,
        "order": {"id": ORDER, "merchant_order_id": REFERENCE},
        "is_live": False,
        "created_at": "2026-09-27T09:35:10.000000",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }
    if refunded_cents is not None:
        body["refunded_amount_cents"] = refunded_cents
    return body


def child(*, transaction: int = SECOND_CHILD, amount_cents: int = 6900) -> dict[str, Any]:
    """A refund transaction itself - what Transaction Inquiry answers after a refund."""
    return {
        "id": transaction,
        "pending": False,
        "amount_cents": amount_cents,
        "refunded_amount_cents": 0,
        "success": True,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": False,
        "is_voided": False,
        "is_refunded": False,
        "is_refund": True,
        "is_void": False,
        "is_3d_secure": False,
        "integration_id": INTEGRATION,
        "has_parent_transaction": True,
        "parent_transaction": PARENT,
        "order": {"id": ORDER, "merchant_order_id": REFERENCE},
        "is_live": False,
        "created_at": "2026-09-27T09:37:27.000000",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }


def collection(*, success: bool = True) -> dict[str, Any]:
    """The payment before any refund: what its first callback carries."""
    body = parent(refunded_cents=None, is_refunded=False)
    body["success"] = success
    body["error_occured"] = not success
    return body


def provider(transport: httpx.MockTransport | None = None) -> PaymobProvider:
    return PaymobProvider(
        secret_key="egy_sk_test_semantics",
        public_key="egy_pk_test_semantics",
        hmac_secret=HMAC_SECRET,
        integration_ids=[INTEGRATION],
        api_key="an-inquiry-api-key",
        transport=transport,
    )


def verified(transaction: dict[str, Any]) -> Any:
    body = json.dumps({"type": "TRANSACTION", "obj": transaction}).encode("utf-8")
    return provider().verify_callback(
        payload=body, signature=hmac_signature(transaction, secret=HMAC_SECRET)
    )


class Paymob:
    """The inquiry APIs at the socket: an order's latest transaction, and reads by id."""

    def __init__(
        self,
        *,
        latest: dict[str, Any] | None,
        transactions: dict[int, dict[str, Any]] | None = None,
        read_status: int = 200,
    ) -> None:
        self.latest = latest
        self.transactions = transactions or {}
        self.read_status = read_status
        self.seen: list[tuple[str, str, bool]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append(
            (request.method, path, request.headers.get("Authorization") == "Bearer bearer")
        )
        if path.endswith("/api/auth/tokens"):
            return httpx.Response(201, json={"token": "bearer"})
        if path.endswith("/transaction_inquiry"):
            return httpx.Response(200, json=self.latest or {})
        if "/api/acceptance/transactions/" in path:
            if self.read_status != 200:
                return httpx.Response(self.read_status, json={"detail": "no"})
            found = self.transactions.get(int(path.rsplit("/", 1)[-1]))
            if found is None:
                return httpx.Response(404, json={"detail": "Not found."})
            return httpx.Response(200, json=found)
        raise AssertionError(f"unexpected request {request.method} {path}")

    def provider(self) -> PaymobProvider:
        return provider(httpx.MockTransport(self.handler))


# ------------------------------------------------ PAY-E2E-01: callback identity


def test_each_running_total_of_one_parent_is_its_own_event() -> None:
    """R1/R2: 30 then 99 on the same parent are two events, not one and a duplicate."""
    first = verified(parent(refunded_cents=3000))
    second = verified(parent(refunded_cents=9900))

    assert (first.kind, second.kind) == (EventKind.REFUNDED, EventKind.REFUNDED)
    assert first.provider_transaction_id == second.provider_transaction_id == str(PARENT)
    assert first.event_id == f"{PARENT}:refunded:3000"
    assert second.event_id == f"{PARENT}:refunded:9900"
    assert (first.refunded_amount, second.refunded_amount) == (Decimal("30"), Decimal("99"))
    assert not first.reversal_child and not second.reversal_child


def test_the_same_running_total_is_the_same_event() -> None:
    """R3/R4: a replay of one cumulative state keys to itself, so it stays a duplicate."""
    assert (
        verified(parent(refunded_cents=3000)).event_id
        == verified(parent(refunded_cents=3000)).event_id
    )


def test_a_refund_is_never_keyed_like_the_collection_it_reverses() -> None:
    assert verified(collection()).event_id == f"{PARENT}:succeeded"
    assert verified(parent(refunded_cents=9900)).event_id != f"{PARENT}:succeeded"


def test_a_void_is_keyed_on_the_whole_amount() -> None:
    voided = parent(refunded_cents=None, is_refunded=False)
    voided["is_voided"] = True
    event = verified(voided)
    assert event.kind is EventKind.VOIDED
    assert event.event_id == f"{PARENT}:voided:9900"


# ------------------------------------------- PAY-E2E-03: what a child means


def test_i1_a_clean_collection_is_a_success() -> None:
    event = verified(collection())
    assert event.kind is EventKind.SUCCEEDED and event.amount == Decimal("99")
    assert not event.reversal_child


def test_i2_a_failed_collection_is_a_failure() -> None:
    assert verified(collection(success=False)).kind is EventKind.FAILED


def test_i3_a_refund_child_is_never_a_collection() -> None:
    """`success: true` on a refund is the refund's success, not money arriving."""
    event = verified(child())

    assert event.kind is not EventKind.SUCCEEDED
    assert not event.succeeded
    assert event.is_reversal and event.reversal_child
    assert event.amount == Decimal("69")
    # The child's own zero is not a running total and must not read as one.
    assert event.refunded_amount is None
    assert event.event_id == f"{SECOND_CHILD}:refunded"


def test_i4_a_refund_child_names_the_parent_it_reverses() -> None:
    event = verified(child())
    assert event.parent_transaction_id == str(PARENT)
    assert event.provider_transaction_id == str(SECOND_CHILD)
    assert event.order_id == str(ORDER)


def test_a_success_with_a_parent_but_no_refund_flag_is_still_not_a_collection() -> None:
    """This integration never authorises and captures apart, so no payment of ours has
    a parent. A child without `is_refund` is refused as a collection all the same."""
    unflagged = child()
    unflagged["is_refund"] = False
    event = verified(unflagged)
    assert event.kind is not EventKind.SUCCEEDED and event.reversal_child


def test_a_void_child_is_a_reversal_child() -> None:
    void = child()
    void["is_refund"] = False
    void["is_void"] = True
    event = verified(void)
    assert (event.kind, event.reversal_child) == (EventKind.VOIDED, True)


# --------------------------------------------- inquiry: the child's parent


async def test_i5_inquiry_answering_a_full_refund_child_reads_the_parent_total() -> None:
    paymob = Paymob(latest=child(), transactions={PARENT: parent(refunded_cents=9900)})

    answer = await paymob.provider().inquire_charge(REFERENCE)

    assert answer.verdict is InquiryVerdict.ANSWERED and answer.event is not None
    event = answer.event
    assert event.kind is EventKind.REFUNDED and not event.reversal_child
    assert event.provider_transaction_id == str(PARENT)
    assert event.refunded_amount == Decimal("99")
    # The same identity the parent's own refund callback carries, so a callback
    # and a reconciliation racing each other apply it once.
    assert event.event_id == verified(parent(refunded_cents=9900)).event_id
    # Read by id with the inquiry bearer token, as a GET.
    assert ("GET", f"/api/acceptance/transactions/{PARENT}", True) in paymob.seen


async def test_i6_inquiry_answering_a_partial_refund_child_reads_the_partial_total() -> None:
    paymob = Paymob(
        latest=child(transaction=FIRST_CHILD, amount_cents=3000),
        transactions={PARENT: parent(refunded_cents=3000)},
    )
    answer = await paymob.provider().inquire_charge(REFERENCE)
    assert answer.event is not None and answer.event.refunded_amount == Decimal("30")
    assert answer.event.kind is EventKind.REFUNDED


async def test_a_child_whose_parent_does_not_show_the_refund_yet_is_asked_again() -> None:
    """Two provider statements that disagree are never settled on - least of all as
    the parent's `success` read as a collection of money on its way back."""
    paymob = Paymob(
        latest=child(), transactions={PARENT: parent(refunded_cents=0, is_refunded=False)}
    )
    answer = await paymob.provider().inquire_charge(REFERENCE)
    assert answer.verdict is InquiryVerdict.PENDING
    assert answer.event is None


@pytest.mark.parametrize("status", [500, 404])
async def test_a_child_whose_parent_cannot_be_read_learns_nothing(status: int) -> None:
    paymob = Paymob(latest=child(), read_status=status)
    answer = await paymob.provider().inquire_charge(REFERENCE)
    assert answer.verdict is InquiryVerdict.UNREACHABLE
    assert answer.event is None


async def test_a_parent_on_another_order_is_not_believed() -> None:
    elsewhere = parent(refunded_cents=9900)
    elsewhere["order"] = {"id": ORDER + 1, "merchant_order_id": "someone-else"}
    paymob = Paymob(latest=child(), transactions={PARENT: elsewhere})
    answer = await paymob.provider().inquire_charge(REFERENCE)
    assert answer.verdict is InquiryVerdict.UNREACHABLE and answer.event is None


async def test_an_ordinary_inquiry_still_answers_the_collection() -> None:
    """The fix must not stop lost-callback recovery of a normal payment."""
    paymob = Paymob(latest=collection())
    answer = await paymob.provider().inquire_charge(REFERENCE)
    assert answer.verdict is InquiryVerdict.ANSWERED and answer.event is not None
    assert answer.event.kind is EventKind.SUCCEEDED
    assert not any("/api/acceptance/transactions/" in path for _, path, _ in paymob.seen)


async def test_a_transaction_read_answering_about_another_id_is_not_an_answer() -> None:
    paymob = Paymob(latest=None, transactions={PARENT: {**parent(refunded_cents=9900), "id": 1}})
    answer = await paymob.provider().inquire_transaction(str(PARENT))
    assert answer.verdict is InquiryVerdict.NOT_FOUND


async def test_a_transaction_read_needs_the_inquiry_credential() -> None:
    no_key = PaymobProvider(
        secret_key="egy_sk_test_semantics",
        public_key="egy_pk_test_semantics",
        hmac_secret=HMAC_SECRET,
        integration_ids=[INTEGRATION],
    )
    assert (await no_key.inquire_transaction(str(PARENT))).verdict is InquiryVerdict.UNSUPPORTED


async def test_a_transaction_read_refuses_an_id_that_is_not_a_number() -> None:
    paymob = Paymob(latest=None)
    answer = await paymob.provider().inquire_transaction("../orders")
    assert answer.verdict is InquiryVerdict.NOT_FOUND
    assert paymob.seen == []
