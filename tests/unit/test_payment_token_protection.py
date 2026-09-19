"""Security properties of the reusable Paymob card credential boundary."""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

import pytest

from app.core.crypto import CredentialDecryptionError, generate_key
from app.db.models.payment_method import PaymentMethod
from app.integrations.billing.checkout import SavedMethodCharge, SavedPaymentMethod
from app.services.payment_token_service import PaymentTokenProtector
from tests.payment_tokens import ENCRYPTION_KEY, FINGERPRINT_KEY, PROTECTOR

TOKEN = "synthetic-paymob-card-token"


def _method(*, tenant_id: uuid.UUID, method_id: uuid.UUID, ciphertext: str) -> PaymentMethod:
    return PaymentMethod(
        id=method_id,
        tenant_id=tenant_id,
        provider="paymob",
        provider_token=ciphertext,
        token_fingerprint=PROTECTOR.fingerprint(provider="paymob", token=TOKEN),
    )


def test_encryption_is_randomized_while_keyed_lookup_is_stable() -> None:
    tenant_id, method_id = uuid.uuid4(), uuid.uuid4()
    first = PROTECTOR.seal(TOKEN, provider="paymob", tenant_id=tenant_id, method_id=method_id)
    second = PROTECTOR.seal(TOKEN, provider="paymob", tenant_id=tenant_id, method_id=method_id)
    assert first.ciphertext != second.ciphertext
    assert TOKEN not in first.ciphertext
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != hashlib.sha256(TOKEN.encode()).hexdigest()
    assert (
        PROTECTOR.open(
            _method(tenant_id=tenant_id, method_id=method_id, ciphertext=first.ciphertext)
        )
        == TOKEN
    )


def test_ciphertext_cannot_be_moved_to_another_row_or_used_as_plaintext() -> None:
    tenant_id, method_id = uuid.uuid4(), uuid.uuid4()
    protected = PROTECTOR.seal(TOKEN, provider="paymob", tenant_id=tenant_id, method_id=method_id)
    with pytest.raises(CredentialDecryptionError):
        PROTECTOR.open(
            _method(tenant_id=uuid.uuid4(), method_id=method_id, ciphertext=protected.ciphertext)
        )
    with pytest.raises(CredentialDecryptionError):
        PROTECTOR.open(
            _method(tenant_id=tenant_id, method_id=uuid.uuid4(), ciphertext=protected.ciphertext)
        )
    with pytest.raises(CredentialDecryptionError):
        PROTECTOR.open(_method(tenant_id=tenant_id, method_id=method_id, ciphertext=TOKEN))


def test_encryption_key_rotation_preserves_old_cards_and_new_writes() -> None:
    tenant_id, method_id = uuid.uuid4(), uuid.uuid4()
    old = PROTECTOR.seal(TOKEN, provider="paymob", tenant_id=tenant_id, method_id=method_id)
    rotated = PaymentTokenProtector(
        encryption_keys=[generate_key(), ENCRYPTION_KEY],
        fingerprint_key=FINGERPRINT_KEY,
    )
    assert (
        rotated.open(_method(tenant_id=tenant_id, method_id=method_id, ciphertext=old.ciphertext))
        == TOKEN
    )
    new = rotated.seal(TOKEN, provider="paymob", tenant_id=tenant_id, method_id=method_id)
    assert new.fingerprint == old.fingerprint
    assert (
        rotated.open(_method(tenant_id=tenant_id, method_id=method_id, ciphertext=new.ciphertext))
        == TOKEN
    )


def test_token_bearing_provider_objects_hide_the_credential_in_repr() -> None:
    saved = SavedPaymentMethod(token=TOKEN, provider_token_id="record-1")
    charge = SavedMethodCharge(
        reference="payment-1", token=TOKEN, amount=Decimal("1"), currency="EGP", description="plan"
    )
    assert TOKEN not in repr(saved)
    assert TOKEN not in repr(charge)
