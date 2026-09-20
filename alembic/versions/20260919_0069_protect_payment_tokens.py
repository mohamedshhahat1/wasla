"""Encrypt existing reusable Paymob card tokens and replace plaintext lookup.

Revision ID: 0069
Revises: 0068

The backfill and DDL share one PostgreSQL transaction. Missing or wrong keys
abort the migration; no mixed plaintext/encrypted schema can be committed.
Downgrade refuses while methods exist because it would expose them again.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

from app.core.crypto import CredentialCipher
from app.services.payment_token_service import PaymentTokenProtector

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_methods", sa.Column("token_fingerprint", sa.String(64), nullable=True))
    op.alter_column(
        "payment_methods",
        "provider_token",
        existing_type=sa.String(200),
        type_=sa.String(512),
        existing_nullable=False,
    )
    connection = op.get_bind()
    count = connection.scalar(sa.text("SELECT count(*) FROM payment_methods"))
    if count:
        keys = [item.strip() for item in os.getenv("CREDENTIAL_ENCRYPTION_KEYS", "").split(",")]
        keys = [item for item in keys if item]
        protector = PaymentTokenProtector(
            encryption_keys=keys,
            fingerprint_key=os.getenv("PAYMENT_TOKEN_FINGERPRINT_KEY"),
        )
        cipher = CredentialCipher(keys)
        last_id = None
        while True:
            if last_id is None:
                page = connection.execute(
                    sa.text(
                        "SELECT id, tenant_id, provider, provider_token "
                        "FROM payment_methods ORDER BY id LIMIT 500"
                    )
                ).all()
            else:
                page = connection.execute(
                    sa.text(
                        "SELECT id, tenant_id, provider, provider_token "
                        "FROM payment_methods WHERE id > :last_id ORDER BY id LIMIT 500"
                    ),
                    {"last_id": last_id},
                ).all()
            if not page:
                break
            for method_id, tenant_id, provider, stored in page:
                if not stored:
                    raise RuntimeError("A saved payment method has no reusable token.")
                context = protector.context(provider, tenant_id, method_id)
                # A previous interrupted attempt cannot commit partial data,
                # but recognise a controlled manual backfill if one exists.
                token = (
                    cipher.decrypt(stored, context=context) if stored.startswith("v1.") else stored
                )
                protected = protector.seal(
                    token,
                    provider=provider,
                    tenant_id=tenant_id,
                    method_id=method_id,
                )
                connection.execute(
                    sa.text(
                        "UPDATE payment_methods SET provider_token = :ciphertext, "
                        "token_fingerprint = :fingerprint WHERE id = :id"
                    ),
                    {
                        "ciphertext": protected.ciphertext,
                        "fingerprint": protected.fingerprint,
                        "id": method_id,
                    },
                )
            last_id = page[-1].id
    op.alter_column("payment_methods", "token_fingerprint", nullable=False)
    op.drop_constraint(
        "uq_payment_methods_provider_provider_token", "payment_methods", type_="unique"
    )
    op.create_unique_constraint(
        "uq_payment_methods_token_fingerprint",
        "payment_methods",
        ["provider", "token_fingerprint"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.scalar(sa.text("SELECT count(*) FROM payment_methods"))
    if count:
        raise RuntimeError(
            "Refusing to downgrade protected payment tokens to plaintext. "
            "Restore the prior application release with the protected schema."
        )
    op.drop_constraint("uq_payment_methods_token_fingerprint", "payment_methods", type_="unique")
    op.create_unique_constraint(
        "uq_payment_methods_provider_provider_token",
        "payment_methods",
        ["provider", "provider_token"],
    )
    op.drop_column("payment_methods", "token_fingerprint")
    op.alter_column(
        "payment_methods",
        "provider_token",
        existing_type=sa.String(512),
        type_=sa.String(200),
        existing_nullable=False,
    )
