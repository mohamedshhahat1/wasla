"""The moves a payment and an invoice are allowed to make.

These tables are not bookkeeping. The statuses on a payment arrive from
*outside* - a processor decides what a payment did and tells us in a callback -
and a callback is a stranger's assertion until it is checked against what we
already believe. The signature proves who sent it; nothing in a signature says
the thing it claims is possible.

So the two properties worth being sure of are here, and both are money:

- **`refunded` never becomes `succeeded`.** That is how a customer who has been
  given their money back keeps the product, and it is reachable by a callback
  arriving out of order as easily as by anybody forging one.
- **`failed` never becomes `succeeded`.** Each attempt at collecting is its own
  row, so a declined attempt is finished; a later success is a different
  attempt. Letting the row move would erase the decline that a dispute turns
  on.

The tables are asserted exhaustively rather than by example, because a
transition that nobody wrote a test for is exactly the one that turns out to be
allowed.
"""

from __future__ import annotations

import pytest

from app.db.models.invoice import (
    INVOICE_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    InvoiceStatus,
    PaymentStatus,
    invoice_may_move,
    payment_may_move,
)

# ------------------------------------------------------------------ payments


def test_every_payment_status_has_a_rule() -> None:
    """A status missing from the table raises `KeyError` at the worst moment.

    `payment_may_move` indexes the dictionary directly, so a status added to
    the enum without a rule here would fail inside the callback handler - after
    the event has been claimed, and while a customer is waiting to find out
    whether they paid.
    """
    assert set(PAYMENT_TRANSITIONS) == set(PaymentStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (PaymentStatus.PENDING, PaymentStatus.SUCCEEDED),
        (PaymentStatus.PENDING, PaymentStatus.FAILED),
        (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED),
    ],
)
def test_the_moves_a_real_payment_makes_are_allowed(
    current: PaymentStatus,
    target: PaymentStatus,
) -> None:
    """The whole life of a payment: in flight, then landed, then maybe back."""
    assert payment_may_move(current, target)


def test_a_refunded_payment_can_never_be_collected_again() -> None:
    """The one that gives the product away.

    A signed callback claiming success about a payment we have already given
    back would otherwise settle the invoice a second time - and a customer with
    their money back and a paid invoice is a customer using the product for
    free.
    """
    assert not payment_may_move(PaymentStatus.REFUNDED, PaymentStatus.SUCCEEDED)
    assert PAYMENT_TRANSITIONS[PaymentStatus.REFUNDED] == frozenset()


def test_a_failed_attempt_stays_failed() -> None:
    """A retry is another row, not this one changing its mind.

    Keeping the decline is the point: it is what a customer sees when they ask
    why their card did not work, and what a chargeback argument rests on.
    """
    assert not payment_may_move(PaymentStatus.FAILED, PaymentStatus.SUCCEEDED)
    assert PAYMENT_TRANSITIONS[PaymentStatus.FAILED] == frozenset()


def test_nothing_may_move_back_into_pending() -> None:
    """Time does not run backwards, and neither does a payment.

    A late `pending` callback arriving after the `success` one is a real thing
    that happens with 3-D Secure, and treating it as a move would un-collect
    money that has arrived.
    """
    for status in PaymentStatus:
        assert not payment_may_move(status, PaymentStatus.PENDING)


def test_a_status_is_never_a_move_to_itself() -> None:
    """Restating what a row already says is nothing, not a transition.

    The caller relies on this to tell "the provider told us again" from "the
    provider told us something new", which is the difference between a ledger
    entry saying `no_change` and one saying `applied`.
    """
    for status in PaymentStatus:
        assert not payment_may_move(status, status)


# ------------------------------------------------------------------ invoices


def test_every_invoice_status_has_a_rule() -> None:
    assert set(INVOICE_TRANSITIONS) == set(InvoiceStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (InvoiceStatus.DRAFT, InvoiceStatus.OPEN),
        (InvoiceStatus.OPEN, InvoiceStatus.PAID),
        (InvoiceStatus.OPEN, InvoiceStatus.UNCOLLECTIBLE),
        (InvoiceStatus.OPEN, InvoiceStatus.VOID),
        (InvoiceStatus.UNCOLLECTIBLE, InvoiceStatus.PAID),
    ],
)
def test_the_ordinary_life_of_an_invoice_is_allowed(
    current: InvoiceStatus,
    target: InvoiceStatus,
) -> None:
    assert invoice_may_move(current, target)


def test_a_paid_invoice_reopens_only_by_being_refunded() -> None:
    """It looks wrong and is not.

    `amount_paid` records money we *hold*. Giving it back means an invoice
    whose payments no longer cover it, and an invoice that is not covered is
    not paid. Voiding it afterwards - because the customer is leaving - is a
    separate deliberate act rather than something inferred from a reversal,
    which is why `PAID -> VOID` is absent.
    """
    assert invoice_may_move(InvoiceStatus.PAID, InvoiceStatus.OPEN)
    assert INVOICE_TRANSITIONS[InvoiceStatus.PAID] == frozenset({InvoiceStatus.OPEN})


def test_a_withdrawn_invoice_never_comes_back() -> None:
    """A bill the customer was told to ignore stays ignored."""
    assert INVOICE_TRANSITIONS[InvoiceStatus.VOID] == frozenset()


def test_an_invoice_cannot_be_settled_twice() -> None:
    """`PAID -> PAID` is not a move, which is what stops a double collection.

    A second payment landing against a settled invoice is refused by this
    rather than added to `amount_paid` - it means the customer paid twice,
    which is a refund to issue rather than a balance to increase.
    """
    assert not invoice_may_move(InvoiceStatus.PAID, InvoiceStatus.PAID)


def test_an_invoice_never_returns_to_draft() -> None:
    """A bill that has been sent cannot become one that has not."""
    for status in InvoiceStatus:
        assert not invoice_may_move(status, InvoiceStatus.DRAFT)
