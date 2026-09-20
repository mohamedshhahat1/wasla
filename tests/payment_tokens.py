"""Non-production encryption material for saved-card integration fixtures."""

from __future__ import annotations

import base64
import uuid
from typing import Any

from app.core.config import Settings
from app.db.models.payment_method import PaymentMethod
from app.services.payment_token_service import PaymentTokenProtector

ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()
FINGERPRINT_KEY = base64.b64encode(bytes(range(32, 64))).decode()
SETTINGS = Settings(
    _env_file=None,
    environment="test",
    credential_encryption_keys=[ENCRYPTION_KEY],
    payment_token_fingerprint_key=FINGERPRINT_KEY,
)
PROTECTOR = PaymentTokenProtector.from_settings(SETTINGS)


def saved_card(
    *, tenant_id: uuid.UUID, token: str, provider: str = "paymob", **metadata: Any
) -> PaymentMethod:
    method_id = uuid.uuid4()
    protected = PROTECTOR.seal(token, provider=provider, tenant_id=tenant_id, method_id=method_id)
    return PaymentMethod(
        id=method_id,
        tenant_id=tenant_id,
        provider=provider,
        provider_token=protected.ciphertext,
        token_fingerprint=protected.fingerprint,
        **metadata,
    )
