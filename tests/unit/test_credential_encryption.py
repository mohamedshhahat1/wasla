"""Encryption at rest for workspace credentials.

ADR-009 refused to store a Meta token at all until this existed. The tests that
matter are therefore the ones that make the column safe to have: a ciphertext
cannot be moved between workspaces, tampering is detected rather than decrypted,
a retired key is refused rather than silently skipped, and nothing anywhere
prints the plaintext.
"""

from __future__ import annotations

import ast
import base64
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.crypto import (
    CONNECTION_BOUND,
    CredentialCipher,
    CredentialDecryptionError,
    generate_key,
    key_id,
)
from app.core.exceptions import DependencyUnavailableError, ValidationError
from app.db.models.channel import Channel
from app.db.models.whatsapp import WhatsAppAccount
from app.services.credential_service import (
    CredentialService,
    build_cipher,
    connection_context,
)

TENANT = str(uuid.uuid4())
OTHER_TENANT = str(uuid.uuid4())
TOKEN = "EAAG-a-real-looking-meta-token"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
    }
    values.update(overrides)
    return Settings(**values)


# ------------------------------------------------------------------- the key


def test_a_generated_key_is_the_right_size() -> None:
    assert len(base64.b64decode(generate_key())) == 32


def test_a_short_key_is_refused_at_construction() -> None:
    """A deployment configured with a bad key should fail to start, not fail the
    first time a customer connects a number."""
    short = base64.b64encode(b"too-short").decode()

    with pytest.raises(DependencyUnavailableError):
        CredentialCipher([short])


def test_a_key_that_is_not_base64_is_refused() -> None:
    with pytest.raises(DependencyUnavailableError):
        CredentialCipher(["not base64 at all!!"])


def test_no_key_at_all_is_refused_rather_than_ignored() -> None:
    """A cipher that quietly stored plaintext when misconfigured would be worse
    than no encryption, because nothing would look wrong."""
    with pytest.raises(DependencyUnavailableError):
        CredentialCipher([])


# ---------------------------------------------------------------- the scheme


def test_a_credential_survives_a_round_trip() -> None:
    cipher = CredentialCipher([generate_key()])

    sealed = cipher.encrypt(TOKEN, context=TENANT)

    assert cipher.decrypt(sealed, context=TENANT) == TOKEN


def test_the_stored_value_does_not_contain_the_token() -> None:
    cipher = CredentialCipher([generate_key()])

    sealed = cipher.encrypt(TOKEN, context=TENANT)

    assert TOKEN not in sealed
    assert sealed.startswith("v1.")


def test_the_same_token_encrypts_differently_every_time() -> None:
    """A random nonce per encryption, so two workspaces with the same token do
    not have the same ciphertext - which would be visible in any dump."""
    cipher = CredentialCipher([generate_key()])

    first = cipher.encrypt(TOKEN, context=TENANT)
    second = cipher.encrypt(TOKEN, context=TENANT)

    assert first != second


def test_a_ciphertext_cannot_be_moved_to_another_workspace() -> None:
    """The attack a column full of tokens invites: copy one row's credential
    into another workspace's row. The tenant is authenticated data, so the
    bytes are useless anywhere but where they were written."""
    cipher = CredentialCipher([generate_key()])
    sealed = cipher.encrypt(TOKEN, context=TENANT)

    with pytest.raises(CredentialDecryptionError):
        cipher.decrypt(sealed, context=OTHER_TENANT)


def test_tampering_is_detected_rather_than_decrypted() -> None:
    """GCM is authenticated. An unauthenticated mode would decrypt
    attacker-controlled bytes into something."""
    cipher = CredentialCipher([generate_key()])
    sealed = cipher.encrypt(TOKEN, context=TENANT)
    version, identifier, nonce, ciphertext = sealed.split(".")
    tampered = ".".join((version, identifier, nonce, ciphertext[:-4] + "AAAA"))

    with pytest.raises(CredentialDecryptionError):
        cipher.decrypt(tampered, context=TENANT)


def test_a_malformed_envelope_is_refused() -> None:
    cipher = CredentialCipher([generate_key()])

    for broken in ("", "nonsense", "v1.only.three", "v9.a.b.c", "v3.a.b.c"):
        with pytest.raises(CredentialDecryptionError):
            cipher.decrypt(broken, context=TENANT)


# --------------------------------------------------------------- the key ring


def test_an_old_key_still_decrypts_after_a_new_one_is_added() -> None:
    """Rotation is the half of "encryption at rest" that gets skipped and then
    cannot be added afterwards."""
    old, new = generate_key(), generate_key()
    sealed = CredentialCipher([old]).encrypt(TOKEN, context=TENANT)

    rotated = CredentialCipher([new, old])

    assert rotated.decrypt(sealed, context=TENANT) == TOKEN


def test_a_new_credential_uses_the_new_key() -> None:
    old, new = generate_key(), generate_key()
    rotated = CredentialCipher([new, old])

    sealed = rotated.encrypt(TOKEN, context=TENANT)

    assert sealed.split(".")[1] == key_id(base64.b64decode(new))


def test_a_retired_key_is_refused_rather_than_guessed() -> None:
    """Loudly unusable beats silently wrong: the credential needs re-entering,
    and pretending otherwise would send with the wrong identity."""
    old, new = generate_key(), generate_key()
    sealed = CredentialCipher([old]).encrypt(TOKEN, context=TENANT)

    with pytest.raises(CredentialDecryptionError):
        CredentialCipher([new]).decrypt(sealed, context=TENANT)


def test_reordering_the_ring_does_not_break_existing_ciphertexts() -> None:
    """The envelope names the key by digest, not by position."""
    first, second = generate_key(), generate_key()
    sealed = CredentialCipher([first, second]).encrypt(TOKEN, context=TENANT)

    assert CredentialCipher([second, first]).decrypt(sealed, context=TENANT) == TOKEN


def test_a_ciphertext_written_with_an_older_key_is_flagged_for_rotation() -> None:
    old, new = generate_key(), generate_key()
    sealed = CredentialCipher([old]).encrypt(TOKEN, context=TENANT)
    rotated = CredentialCipher([new, old])

    assert rotated.needs_rotation(sealed) is True
    assert rotated.needs_rotation(rotated.encrypt(TOKEN, context=TENANT)) is False


# ------------------------------------------------------------- the service


def test_a_deployment_without_a_key_cannot_store_a_credential() -> None:
    """It refuses rather than storing plaintext "for now", which is exactly how
    a plaintext token column comes to exist."""
    service = CredentialService(_settings())

    assert service.can_store is False
    with pytest.raises(ValidationError):
        service.seal(
            TOKEN, tenant_id=uuid.uuid4(), connection_id=uuid.uuid4(), channel=Channel.WHATSAPP
        )


def test_a_workspace_without_its_own_token_uses_the_platform_credential() -> None:
    """How every workspace worked before this column existed, and how a new one
    works until it supplies one."""
    service = CredentialService(_settings(meta_access_token="platform-token"))
    account = WhatsAppAccount(tenant_id=uuid.uuid4(), access_token_encrypted=None)

    resolved = service.resolve(account)

    assert resolved.token == "platform-token"
    assert resolved.is_own is False


def test_a_workspace_with_its_own_token_sends_as_itself() -> None:
    key = generate_key()
    settings = _settings(meta_access_token="platform-token", credential_encryption_keys=[key])
    service = CredentialService(settings)
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    account = WhatsAppAccount(
        id=account_id,
        tenant_id=tenant_id,
        access_token_encrypted=service.seal(
            TOKEN, tenant_id=tenant_id, connection_id=account_id, channel=Channel.WHATSAPP
        ),
    )

    resolved = service.resolve(account)

    assert resolved.token == TOKEN
    assert resolved.is_own is True


def test_an_unreadable_credential_does_not_fall_back_to_the_platform() -> None:
    """Sending as the platform when the workspace asked to send as itself is a
    different act with a different sender identity. Doing it quietly because a
    key was rotated badly is the kind of failure nobody notices until a
    customer does."""
    settings = _settings(meta_access_token="platform-token")
    service = CredentialService(settings)
    account = WhatsAppAccount(
        tenant_id=uuid.uuid4(),
        access_token_encrypted="v1.deadbeef.AAAA.AAAA",
    )

    with pytest.raises(CredentialDecryptionError):
        service.resolve(account)


def test_a_resolved_credential_does_not_print_its_token() -> None:
    """`repr` reaches logs, tracebacks and debuggers."""
    service = CredentialService(_settings(meta_access_token="platform-token"))
    resolved = service.resolve(WhatsAppAccount(tenant_id=uuid.uuid4()))

    assert "platform-token" not in repr(resolved)


def test_no_cipher_is_built_when_no_key_is_configured() -> None:
    assert build_cipher(_settings()) is None


def test_a_cipher_is_built_when_a_key_is_configured() -> None:
    assert build_cipher(_settings(credential_encryption_keys=[generate_key()])) is not None


# ------------------------------------------- bound to the connection (OMNI-012)


def _service(key: str) -> CredentialService:
    return CredentialService(_settings(credential_encryption_keys=[key]))


def _number(tenant_id: uuid.UUID, stored: str | None = None) -> WhatsAppAccount:
    return WhatsAppAccount(id=uuid.uuid4(), tenant_id=tenant_id, access_token_encrypted=stored)


def test_a_new_credential_is_sealed_to_its_connection() -> None:
    """Every credential sealed since 0082 is a `v2` envelope, bound to the
    tenant, the connection and the channel together."""
    service = _service(generate_key())
    tenant_id = uuid.uuid4()
    number = _number(tenant_id)

    sealed = service.seal(
        TOKEN, tenant_id=tenant_id, connection_id=number.id, channel=Channel.WHATSAPP
    )

    assert sealed.startswith(f"{CONNECTION_BOUND}.")
    assert TOKEN not in sealed


def test_a_credential_moved_to_another_number_of_the_same_workspace_is_refused() -> None:
    """OMNI-012. The workspace-only binding let a token sealed for one number
    decrypt on any other number of the same workspace - the move the old
    docstring said was impossible. Bound to the connection, it fails."""
    service = _service(generate_key())
    tenant_id = uuid.uuid4()
    first, second = _number(tenant_id), _number(tenant_id)
    first.access_token_encrypted = service.seal(
        TOKEN, tenant_id=tenant_id, connection_id=first.id, channel=Channel.WHATSAPP
    )

    second.access_token_encrypted = first.access_token_encrypted

    assert service.resolve(first).token == TOKEN
    with pytest.raises(CredentialDecryptionError):
        service.resolve(second)


def test_a_workspace_bound_credential_did_move_between_numbers() -> None:
    """The unfixed behaviour, kept as evidence: a `v1` value is bound to the
    workspace only, so it decrypts on a sibling number. It is still read -
    every credential written before 0082 is `v1` - which is why nothing new is
    ever written that way."""
    key = generate_key()
    service = _service(key)
    tenant_id = uuid.uuid4()
    legacy = CredentialCipher([key]).encrypt(TOKEN, context=str(tenant_id))
    first, second = _number(tenant_id, legacy), _number(tenant_id, legacy)

    assert legacy.startswith("v1.")
    assert service.resolve(first).token == TOKEN
    assert service.resolve(second).token == TOKEN


def test_a_workspace_bound_credential_still_cannot_leave_its_workspace() -> None:
    key = generate_key()
    service = _service(key)
    legacy = CredentialCipher([key]).encrypt(TOKEN, context=str(uuid.uuid4()))

    with pytest.raises(CredentialDecryptionError):
        service.resolve(_number(uuid.uuid4(), legacy))


def test_a_connection_bound_credential_cannot_leave_its_workspace() -> None:
    service = _service(generate_key())
    tenant_id = uuid.uuid4()
    number = _number(tenant_id)
    sealed = service.seal(
        TOKEN, tenant_id=tenant_id, connection_id=number.id, channel=Channel.WHATSAPP
    )
    elsewhere = WhatsAppAccount(id=number.id, tenant_id=uuid.uuid4(), access_token_encrypted=sealed)

    with pytest.raises(CredentialDecryptionError):
        service.resolve(elsewhere)


def test_the_channel_is_part_of_what_a_credential_is_bound_to() -> None:
    """A token sealed for a connection on another channel does not decrypt as
    a WhatsApp credential, even on the same ids."""
    key = generate_key()
    tenant_id, connection_id = uuid.uuid4(), uuid.uuid4()
    for_messenger = CredentialCipher([key]).encrypt(
        TOKEN,
        context=connection_context(
            tenant_id=tenant_id, connection_id=connection_id, channel=Channel.MESSENGER
        ),
        version=CONNECTION_BOUND,
    )
    number = WhatsAppAccount(
        id=connection_id, tenant_id=tenant_id, access_token_encrypted=for_messenger
    )

    with pytest.raises(CredentialDecryptionError):
        _service(key).resolve(number)


def test_a_rewritten_version_label_does_not_rebind_a_credential() -> None:
    """The version only says which context to ask for. Relabelling a `v2`
    value as `v1` makes the service ask for the workspace context - which the
    ciphertext was never bound to, so it still fails."""
    service = _service(generate_key())
    tenant_id = uuid.uuid4()
    number = _number(tenant_id)
    sealed = service.seal(
        TOKEN, tenant_id=tenant_id, connection_id=number.id, channel=Channel.WHATSAPP
    )
    relabelled = "v1" + sealed[len(CONNECTION_BOUND) :]

    with pytest.raises(CredentialDecryptionError):
        service.resolve(_number(tenant_id, relabelled))


def test_an_unknown_envelope_version_is_never_written() -> None:
    with pytest.raises(ValidationError):
        CredentialCipher([generate_key()]).encrypt(TOKEN, context=TENANT, version="v3")


def test_only_the_whatsapp_credential_path_reads_the_platform_token() -> None:
    """OMNI-012: the platform token is a WhatsApp system-user token, and the
    fallback to it is WhatsApp's alone. It is read in exactly one place - the
    resolver that takes a `WhatsAppAccount` - so no other channel's send can
    reach for it. Read means an attribute access in code; a docstring naming
    it, or `config.py` declaring it, is not one."""
    root = Path(__file__).resolve().parents[2] / "app"
    readers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if any(
            isinstance(node, ast.Attribute) and node.attr == "meta_access_token"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )

    assert readers == ["services/credential_service.py"]
