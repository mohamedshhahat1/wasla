"""What a Google login is allowed to change about an account.

Three fields arrive with every token - name, picture, address - and they are
governed by two different rules. Name and picture are decoration and follow
Google. The address is identity and does not, ever, after enrolment.

That asymmetry is the whole subject of this file. It is invisible when reading
`_refresh_profile`, because the interesting part is the line that is *not*
there, and a future reader tidying up by "finishing" the method would open an
account-takeover path that no other test in the suite would notice.
"""

from __future__ import annotations

import pytest

from app.db.models.user import User
from app.integrations.google.oidc import GoogleIdentityClaims
from app.services.google_auth_service import GoogleAuthService

SUBJECT = "109876543210987654321"
EMAIL = "person@example.com"
PICTURE = "https://lh3.googleusercontent.com/a/abc123=s96-c"


def _claims(**overrides) -> GoogleIdentityClaims:
    values = {
        "subject": SUBJECT,
        "email": EMAIL,
        "email_verified": True,
        "full_name": "A Person",
        "picture": PICTURE,
    }
    values.update(overrides)
    return GoogleIdentityClaims(**values)


def _user(**overrides) -> User:
    """A detached `User`, which is all this method touches.

    `_refresh_profile` is a static method over an ORM object and a frozen claims
    record: no session, no flush, no network. Testing it against a real `User`
    rather than a stand-in is what makes the column names in the assertions
    below real - a rename would fail here rather than pass against a mock.
    """
    values = {"email": EMAIL, "full_name": None, "avatar_url": None}
    values.update(overrides)
    return User(**values)


def _refresh(user: User, claims: GoogleIdentityClaims) -> None:
    GoogleAuthService._refresh_profile(user=user, claims=claims)


def test_a_first_refresh_fills_an_empty_profile():
    user = _user()
    _refresh(user, _claims())
    assert user.full_name == "A Person"
    assert user.avatar_url == PICTURE


def test_a_changed_google_profile_is_followed():
    """The point of refreshing at all, rather than only writing at enrolment."""
    user = _user(full_name="An Old Name", avatar_url="https://example.com/old.png")
    _refresh(user, _claims(full_name="A New Name", picture="https://example.com/new.png"))
    assert user.full_name == "A New Name"
    assert user.avatar_url == "https://example.com/new.png"


@pytest.mark.parametrize(
    ("claim", "column", "kept"),
    [("full_name", "full_name", "Keep Me"), ("picture", "avatar_url", PICTURE)],
)
def test_an_absent_claim_leaves_the_stored_value_alone(claim, column, kept):
    """Google omitting a field is not somebody clearing it.

    Treating the two alike would blank a perfectly good name or avatar every
    time a token happened to arrive without one. Only the absent claim's column
    is asserted: the other one is present in the token and *should* be
    followed, which is the previous test.
    """
    user = _user(full_name="Keep Me", avatar_url=PICTURE)
    _refresh(user, _claims(**{claim: None}))
    assert getattr(user, column) == kept


def test_the_email_address_is_never_rewritten():
    """The security assertion this file exists for.

    `_resume` stops consulting the email claim once an identity row exists, so
    that a Google account which later acquires somebody else's address gains
    nothing by it. If the refresh wrote `claims.email` to `user.email`, control
    of a Google account would become the power to move a Wasla account onto any
    address Google would attest to - and every password reset thereafter would
    go to the new one.
    """
    user = _user(email="original@example.com")
    _refresh(user, _claims(email="attacker-controlled@example.com"))
    assert user.email == "original@example.com"


def test_nothing_but_the_two_display_fields_is_touched():
    """A guard against the refresh quietly growing.

    Named fields rather than a loop, so that adding a column to `User` does not
    silently widen what a login may overwrite.
    """
    user = _user(hashed_password="a-hash", is_active=True, token_version=7)
    _refresh(user, _claims())
    assert user.hashed_password == "a-hash"
    assert user.is_active is True
    assert user.token_version == 7
    assert user.email_verified_at is None
