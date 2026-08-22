"""Encryption at rest for per-workspace credentials.

ADR-009 refused to store a Meta token on the account row until this existed, and
was explicit that lifting the ban required encryption *and* key management
first. This is that work; ADR-034 supersedes it.

The scheme is AES-256-GCM with a random 96-bit nonce, and three details are
load-bearing:

**It is authenticated.** GCM detects tampering, so a ciphertext altered in a
backup or by a SQL injection fails to decrypt rather than yielding a token
somebody chose. Plain AES-CBC would decrypt attacker-controlled bytes into
something.

**The workspace is bound into the ciphertext.** The tenant id is passed as
additional authenticated data, so a ciphertext copied from one account row to
another - the obvious attack once a column of tokens exists - fails to decrypt.
The bytes are useless anywhere but the row they were written for.

**A key ring, not a key.** Rotation is the half of "encryption at rest" that
gets skipped and then cannot be added: the envelope records which key encrypted
it, so a new key can be prepended and old ciphertexts keep decrypting until they
are rewritten. A deployment with one key behaves exactly as if there were no
ring.

The envelope is `v1.<key id>.<nonce>.<ciphertext>`, base64url without padding,
version first so the scheme itself can change later without guessing.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.exceptions import DependencyUnavailableError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

VERSION: Final = "v1"
KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
# Enough of the key's digest to identify it in an envelope without being a
# useful thing to have on its own.
KEY_ID_LENGTH: Final = 8
SEPARATOR: Final = "."


class CredentialDecryptionError(ValidationError):
    """A stored credential could not be read back.

    A tampered ciphertext, a key that has been retired, or an envelope written
    by a version this build does not understand. All three are the same problem
    operationally - the credential is unusable and a person has to fix it - and
    none of them should be confused with "there is no credential".
    """

    message = "A stored credential could not be decrypted."


def _decode_key(raw: str) -> bytes:
    """Read one base64 key, refusing anything that is not a real AES-256 key.

    Refused loudly at construction rather than at first use: a deployment
    configured with a short key should fail to start, not fail the first time a
    customer connects a number.
    """
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (ValueError, TypeError) as error:
        raise DependencyUnavailableError(
            "A credential encryption key is not valid base64."
        ) from error
    if len(key) != KEY_BYTES:
        raise DependencyUnavailableError(
            f"A credential encryption key must be {KEY_BYTES} bytes; got {len(key)}."
        )
    return key


def key_id(key: bytes) -> str:
    """A short, stable identifier for a key.

    A digest rather than a position in a list: an operator who reorders the ring
    must not silently make every existing ciphertext undecryptable.
    """
    return hashlib.sha256(key).hexdigest()[:KEY_ID_LENGTH]


def generate_key() -> str:
    """A fresh key, base64 encoded, for an operator to put in configuration."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


@dataclass(frozen=True, slots=True)
class Envelope:
    """A parsed stored credential."""

    version: str
    key_id: str
    nonce: bytes
    ciphertext: bytes

    @classmethod
    def parse(cls, stored: str) -> Envelope:
        parts = stored.split(SEPARATOR)
        if len(parts) != 4:
            raise CredentialDecryptionError("The stored credential is malformed.")
        version, identifier, nonce, ciphertext = parts
        if version != VERSION:
            raise CredentialDecryptionError(
                f"The stored credential uses an unknown format ({version})."
            )
        try:
            return cls(
                version=version,
                key_id=identifier,
                nonce=_unb64(nonce),
                ciphertext=_unb64(ciphertext),
            )
        except (ValueError, TypeError) as error:
            raise CredentialDecryptionError("The stored credential is malformed.") from error


class CredentialCipher:
    """Encrypts and decrypts workspace credentials.

    Constructed from a key ring: the first key encrypts, every key can decrypt.
    Rotation is therefore prepending a key and, eventually, rewriting the rows
    that still name the old one.
    """

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            # Not a silent no-op. A cipher that quietly stored plaintext when
            # misconfigured would be worse than no encryption at all, because
            # nothing would look wrong.
            raise DependencyUnavailableError("No credential encryption key is configured.")
        self._keys = {key_id(decoded): decoded for decoded in (_decode_key(raw) for raw in keys)}
        self._primary = _decode_key(keys[0])

    @property
    def primary_key_id(self) -> str:
        return key_id(self._primary)

    def encrypt(self, secret: str, *, context: str) -> str:
        """Encrypt one credential, bound to `context`.

        `context` is the tenant id. It is authenticated but not encrypted, so a
        ciphertext moved to another workspace's row fails to decrypt instead of
        working - which is the attack a column full of tokens invites.
        """
        if not secret:
            raise ValidationError("There is no credential to encrypt.")
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self._primary).encrypt(
            nonce,
            secret.encode(),
            context.encode(),
        )
        return SEPARATOR.join(
            (VERSION, self.primary_key_id, _b64(nonce), _b64(ciphertext)),
        )

    def decrypt(self, stored: str, *, context: str) -> str:
        """Read a credential back, or refuse.

        Every failure is the same exception. Distinguishing "wrong key" from
        "tampered" from "wrong workspace" in the error would hand an attacker an
        oracle, and none of the three is separately actionable anyway.
        """
        envelope = Envelope.parse(stored)
        key = self._keys.get(envelope.key_id)
        if key is None:
            logger.warning(
                "credential.unknown_key",
                extra={"event": "credential.unknown_key", "key_id": envelope.key_id},
            )
            raise CredentialDecryptionError(
                "The key this credential was encrypted with is not configured."
            )
        try:
            plaintext = AESGCM(key).decrypt(
                envelope.nonce,
                envelope.ciphertext,
                context.encode(),
            )
        except InvalidTag as error:
            logger.warning(
                "credential.decryption_failed",
                extra={"event": "credential.decryption_failed"},
            )
            raise CredentialDecryptionError() from error
        return plaintext.decode()

    def needs_rotation(self, stored: str) -> bool:
        """Whether this ciphertext was written with a key that is no longer primary.

        What a rotation sweep would read. Nothing calls it yet, and it is here
        because a key ring without a way to find the stragglers is a rotation
        that never finishes.
        """
        return Envelope.parse(stored).key_id != self.primary_key_id
