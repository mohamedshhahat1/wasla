"""The only application boundary that seals and opens reusable card tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass

from app.core.config import Settings
from app.core.crypto import CredentialCipher
from app.core.exceptions import DependencyUnavailableError
from app.db.models.payment_method import PaymentMethod


def _fingerprint_key(raw: str | None) -> bytes:
    if not raw:
        raise DependencyUnavailableError("No payment token fingerprint key is configured.")
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as error:
        raise DependencyUnavailableError("The payment token fingerprint key is invalid.") from error
    if len(key) != 32:
        raise DependencyUnavailableError("The payment token fingerprint key must be 32 bytes.")
    return key


@dataclass(frozen=True, slots=True)
class ProtectedPaymentToken:
    ciphertext: str
    fingerprint: str


class PaymentTokenProtector:
    """AES-GCM with row-bound AAD, plus keyed lookup identity.

    The fingerprint key is independent of the encryption key ring. Rotating the
    encryption key leaves deduplication stable; rotating the fingerprint key
    requires re-fingerprinting all rows before switching readers.
    """

    def __init__(self, *, encryption_keys: list[str], fingerprint_key: str | None) -> None:
        self._cipher = CredentialCipher(encryption_keys)
        self._fingerprint_key = _fingerprint_key(fingerprint_key)

    @classmethod
    def from_settings(cls, settings: Settings) -> PaymentTokenProtector:
        return cls(
            encryption_keys=settings.credential_encryption_keys,
            fingerprint_key=settings.payment_token_fingerprint_key,
        )

    @staticmethod
    def context(provider: str, tenant_id: uuid.UUID, method_id: uuid.UUID) -> str:
        return f"payment-card-token:v1:{provider}:{tenant_id}:{method_id}"

    def fingerprint(self, *, provider: str, token: str) -> str:
        return hmac.new(
            self._fingerprint_key,
            provider.encode("utf-8") + b"\x00" + token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def seal(
        self,
        token: str,
        *,
        provider: str,
        tenant_id: uuid.UUID,
        method_id: uuid.UUID,
    ) -> ProtectedPaymentToken:
        return ProtectedPaymentToken(
            ciphertext=self._cipher.encrypt(
                token,
                context=self.context(provider, tenant_id, method_id),
            ),
            fingerprint=self.fingerprint(provider=provider, token=token),
        )

    def open(self, method: PaymentMethod) -> str:
        return self._cipher.decrypt(
            method.provider_token,
            context=self.context(method.provider, method.tenant_id, method.id),
        )
