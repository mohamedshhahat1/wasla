"""Federated authentication identities.

An identity is an external issuer's assertion that some subject it controls is
the person holding a Wasla account. It lives in its own table rather than as
columns on ``users`` because an account is not "a Google account": it is an
account that Google is willing to vouch for, and over its life it may be
vouched for by more than one issuer, or by none at all.

Nothing in this table is a credential. A row records *that* an issuer knows
this person, and under what subject - never anything that could be replayed to
prove it. No id token, no access token, no Google refresh token. Nothing in the
product calls a Google API on a user's behalf, so a stored Google token would
be a key to a door nobody opens, sitting in the one table an attacker who
reached the database would most want to read (docs/GOOGLE_OAUTH.md, ADR-044).

The provider's copy of the email address is deliberately absent too. It would
be a second copy of personal data that goes stale the moment somebody renames
their Google account, and it would invite exactly the mistake this feature
exists to avoid: resolving an identity by address instead of by subject.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

# OpenID Connect bounds a subject identifier at 255 ASCII characters. Google's
# are 21-digit numbers today; sizing to the standard rather than to the
# observation is what keeps a second issuer from needing a migration.
MAX_PROVIDER_SUBJECT_LENGTH: Final = 255


class IdentityProvider(StrEnum):
    """Issuers whose assertions Wasla will accept.

    One member today. The enum exists so that the second one is a migration
    adding a label rather than a redesign of the table, and so that no code path
    can write a provider name that nothing validated.
    """

    GOOGLE = "google"


IDENTITY_PROVIDER_TYPE = _enum_type(IdentityProvider, name="identity_provider")


class FederatedIdentity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One Wasla account, one issuer, one subject at that issuer.

    The two unique constraints are the identity policy, and they are here rather
    than in a service because a rule enforced in application code is a rule that
    holds only for the code paths somebody remembered.

    ``(provider, provider_subject)`` unique is the important one. It makes "the
    same Google account cannot sign in as two different people" true of the
    data, which is what makes the concurrent first-login race safe: two
    callbacks for one Google subject both find no identity, both insert, and
    exactly one commits. The loser gets an integrity error, which the service
    translates into a retry of the read rather than into a second account.

    ``(user_id, provider)`` unique is the narrower statement that an account
    holds at most one identity per issuer. It settles what "unlink Google" means
    - there is only ever one row to remove - and its index answers "does this
    person have Google attached?", so no separate index on ``user_id`` exists.
    Adding one would be a second copy of the same B-tree.

    ``ondelete="CASCADE"`` is deliberate. ``users`` is soft-deleted in ordinary
    operation, so this fires only on a genuine hard delete - and a surviving
    identity row would then be worse than no row: it would point at nothing, and
    it would still occupy the ``(provider, provider_subject)`` slot that the
    same person needs in order to sign up again.
    """

    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_user_identities_provider_subject",
        ),
        UniqueConstraint(
            "user_id",
            "provider",
            name="uq_user_identities_user_id_provider",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
            name="fk_user_identities_user_id_users",
        ),
        nullable=False,
    )
    provider: Mapped[IdentityProvider] = mapped_column(
        IDENTITY_PROVIDER_TYPE,
        nullable=False,
    )
    # The issuer's stable identifier for this person - Google's `sub`. Never the
    # email address, the name, or the profile picture: those are attributes of
    # an account and every one of them can change while the subject does not.
    provider_subject: Mapped[str] = mapped_column(
        String(MAX_PROVIDER_SUBJECT_LENGTH),
        nullable=False,
    )
    # Last time this identity was actually used to open a session. Nullable
    # because linking an identity is not using it, and a row that has never been
    # signed in with is a real and interesting state - it is what "I connected
    # Google but have not tried it yet" looks like.
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
