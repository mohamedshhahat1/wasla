"""Which token a send uses, and how a workspace's own one is stored.

Two operations and one rule between them: **the plaintext of a credential exists
only inside a single call**. It arrives in a request, is encrypted, and the
plaintext is discarded; it is decrypted at the moment a send needs it and never
held anywhere a later reader could find it. No caller outside this module ever
sees one, which is what makes "never logged, never returned" enforceable rather
than a convention.

Resolution is deliberately a fallback rather than a requirement. A workspace
with its own token sends as itself; one without sends through the platform
credential, exactly as every workspace did before this existed. Making the
per-workspace token mandatory would have broken every deployment on upgrade,
and there is no security gain in it: the platform token is the same secret it
always was.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.config import Settings
from app.core.crypto import CONNECTION_BOUND, CredentialCipher, CredentialDecryptionError
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.db.models.channel import Channel
from app.db.models.whatsapp import WhatsAppAccount

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """The token a send should use, and where it came from.

    `is_own` is recorded because it changes what a failure means: a rejected
    send on a workspace's own token is that workspace's problem to fix, while
    the same rejection on the platform token is everybody's.
    """

    token: str
    is_own: bool

    def __repr__(self) -> str:  # pragma: no cover - stops a token reaching a log
        return f"ResolvedCredential(is_own={self.is_own})"


def connection_context(*, tenant_id: uuid.UUID, connection_id: uuid.UUID, channel: Channel) -> str:
    """What a `v2` credential is bound to: this workspace's connection, on this channel."""
    return f"{tenant_id}:{connection_id}:{channel.value}"


def build_cipher(settings: Settings) -> CredentialCipher | None:
    """The cipher this deployment is configured for, or None.

    None means no key is configured, which is a real and supported state: the
    platform token is used for everything and a workspace attempting to store
    its own is refused. It is not the same as a broken key - that raises at
    construction, because a deployment configured with a bad key should fail to
    start rather than at the first customer who connects a number.
    """
    if not settings.credential_encryption_keys:
        return None
    return CredentialCipher(settings.credential_encryption_keys)


class CredentialService:
    """Stores and resolves per-workspace Meta credentials."""

    def __init__(self, settings: Settings, *, cipher: CredentialCipher | None = None) -> None:
        self._settings = settings
        self._cipher = cipher if cipher is not None else build_cipher(settings)

    @property
    def can_store(self) -> bool:
        return self._cipher is not None

    def seal(
        self,
        token: str,
        *,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        channel: Channel,
    ) -> str:
        """Encrypt a credential for one connection of one workspace (`v2`, OMNI-012).

        Bound to the tenant, the connection and the channel together, so the
        ciphertext decrypts on its own row and nowhere else - not on another
        workspace's row, and not on another connection of the same workspace,
        which the workspace-only binding it replaces still allowed.

        Refused outright when no key is configured. Storing it in the clear
        "for now" is how a plaintext token column comes to exist, and ADR-009
        spent a whole phase refusing exactly that.
        """
        if self._cipher is None:
            raise ValidationError(
                "This deployment cannot store a workspace credential: "
                "no credential encryption key is configured."
            )
        cleaned = token.strip()
        if not cleaned:
            raise ValidationError("The credential is empty.")
        return self._cipher.encrypt(
            cleaned,
            context=connection_context(
                tenant_id=tenant_id, connection_id=connection_id, channel=channel
            ),
            version=CONNECTION_BOUND,
        )

    def resolve(self, account: WhatsAppAccount) -> ResolvedCredential:
        """The token to send this WhatsApp number's messages with.

        A workspace credential that cannot be decrypted does **not** silently
        fall back to the platform token. Sending as the platform when the
        workspace asked to send as itself is a different act with a different
        sender identity, and doing it quietly because a key was rotated badly
        would be the kind of failure nobody notices until a customer does.

        **The platform fallback is WhatsApp's alone.** This takes a
        `WhatsAppAccount` and is reached only through the WhatsApp adapter; a
        connection on any other channel resolves its credential through its own
        adapter, and a WhatsApp system-user token cannot send as a Page or an
        Instagram account anyway (OMNI-012).
        """
        stored = account.access_token_encrypted
        if stored and self._cipher is not None:
            if self._cipher.version_of(stored) == CONNECTION_BOUND:
                context = connection_context(
                    tenant_id=account.tenant_id, connection_id=account.id, channel=Channel.WHATSAPP
                )
            else:
                # Sealed before 0082: bound to the workspace only, and still
                # readable until it is re-sealed.
                context = str(account.tenant_id)
            token = self._cipher.decrypt(stored, context=context)
            return ResolvedCredential(token=token, is_own=True)

        if stored and self._cipher is None:
            # A credential exists and this process cannot read it. Loud, and
            # refused rather than downgraded.
            logger.error(
                "credential.unreadable_without_key",
                extra={
                    "event": "credential.unreadable_without_key",
                    "tenant_id": str(account.tenant_id),
                },
            )
            raise CredentialDecryptionError(
                "This number has its own credential, but no encryption key is configured."
            )

        platform = self._settings.meta_access_token or ""
        return ResolvedCredential(token=platform, is_own=False)
