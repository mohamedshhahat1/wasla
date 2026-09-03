"""Who a Wasla token is for, and what happens to one addressed elsewhere.

SEC-14. Until this claim existed, a Wasla JWT said who issued it, what kind it
was and whose it was — but never who was meant to *accept* it. That was safe
only for as long as one verifier existed, and the audit's recommendation was to
add the claim before a second one did rather than after.

The reproduction is worth writing down, because it is not what it looks like.
Before this change the verifier passed no `audience=` to PyJWT, and PyJWT's
rule when no audience is expected is *"a token carrying `aud` is invalid"*. So
the old behaviour was exactly inverted from the desired one: a token with no
audience was accepted, and a token correctly addressed to `wasla-api` was
refused. Turning the check on is therefore a hard cutover rather than a
tightening — every token minted by the previous release stops working the
moment this one starts. That is stated in the deployment notes and is the
intended cost; the alternative, accepting an absent `aud` for a window, is a
verifier that cannot tell a Wasla token from one minted for somebody else, which
is the whole thing being fixed.

Two audiences rather than one. An access token is presented to the API's
authenticated routes; a refresh token only ever to `/auth/refresh` and
`/auth/logout`. `typ` already separated them, and it still does — this
separates them a second time, inside the library, on the same call that checks
the signature.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.security import (
    ISSUER,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
)

ACCESS_AUDIENCE = "wasla-api"
REFRESH_AUDIENCE = "wasla-auth"

SECRET = "a-signing-secret-long-enough-for-the-validator-to-accept-it"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, environment="test", jwt_secret=SECRET)


def _payload(token: str) -> dict[str, Any]:
    """Every claim, with no verification at all.

    Deliberately unverified: these tests inspect what was *minted*, and
    decoding through the application's own verifier would make an issuance bug
    invisible whenever the matching verification bug cancelled it out.
    """
    decoded: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return decoded


def _claims(**overrides: Any) -> dict[str, Any]:
    """A payload shaped exactly like one this application mints."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": ACCESS_AUDIENCE,
        "sub": str(uuid.uuid4()),
        "typ": TokenType.ACCESS.value,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "ver": 0,
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def _forge(**overrides: Any) -> str:
    return jwt.encode(_claims(**overrides), SECRET, algorithm="HS256")


# ------------------------------------------------------------ what is minted


def test_an_access_token_is_addressed_to_the_api(settings: Settings) -> None:
    token, _ = create_access_token(settings=settings, subject=uuid.uuid4(), token_version=1)

    assert _payload(token)["aud"] == ACCESS_AUDIENCE


def test_a_refresh_token_is_addressed_to_the_auth_endpoints(settings: Settings) -> None:
    token, _ = create_refresh_token(settings=settings, subject=uuid.uuid4(), token_version=1)

    assert _payload(token)["aud"] == REFRESH_AUDIENCE


def test_the_two_kinds_do_not_share_an_audience() -> None:
    """The property the separation rests on, asserted rather than assumed."""
    assert TokenType.ACCESS.audience != TokenType.REFRESH.audience


def test_every_token_type_has_an_audience() -> None:
    """So adding a third kind cannot silently mint one without a claim."""
    for token_type in TokenType:
        assert token_type.audience


def test_an_access_token_minted_for_a_workspace_still_carries_the_audience(
    settings: Settings,
) -> None:
    """The workspace-switch shape: a tenant in the token changes nothing else."""
    token, _ = create_access_token(
        settings=settings,
        subject=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        token_version=3,
    )

    payload = _payload(token)
    assert payload["aud"] == ACCESS_AUDIENCE
    # Pinned together, because the defect this claim is being added beside was
    # a second issuance path that dropped `ver` (ADR-058). Whatever is added to
    # a token has to be added to every path that mints one.
    assert payload["ver"] == 3


# --------------------------------------------------------- what is accepted


def test_a_token_this_application_minted_is_accepted(settings: Settings) -> None:
    """The control. Without it every refusal below could be a broken fixture."""
    subject = uuid.uuid4()
    token, _ = create_access_token(settings=settings, subject=subject, token_version=0)

    assert decode_token(token, settings=settings, expected_type=TokenType.ACCESS).subject == subject


def test_a_refresh_token_is_accepted_at_the_refresh_seam(settings: Settings) -> None:
    subject = uuid.uuid4()
    token, _ = create_refresh_token(settings=settings, subject=subject, token_version=0)

    decoded = decode_token(token, settings=settings, expected_type=TokenType.REFRESH)
    assert decoded.subject == subject


def test_a_hand_built_token_with_the_right_audience_is_accepted(settings: Settings) -> None:
    """The second control: the forgeries below fail for the reason each names."""
    assert decode_token(_forge(), settings=settings, expected_type=TokenType.ACCESS) is not None


# --------------------------------------------------------- what is refused


def test_a_token_with_no_audience_is_refused(settings: Settings) -> None:
    """The old format. Every session minted by the previous release lands here."""
    token = jwt.encode(
        {key: value for key, value in _claims().items() if key != "aud"},
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(AuthenticationError):
        decode_token(token, settings=settings, expected_type=TokenType.ACCESS)


@pytest.mark.parametrize(
    "audience",
    ["wasla-something-else", "wasla", "", "wasla-api ", "WASLA-API", "https://evil.example"],
)
def test_a_token_for_another_audience_is_refused(settings: Settings, audience: str) -> None:
    """Including near-misses, since the check is equality rather than a prefix."""
    with pytest.raises(AuthenticationError):
        decode_token(_forge(aud=audience), settings=settings, expected_type=TokenType.ACCESS)


def test_an_audience_array_is_refused_even_when_it_contains_ours(settings: Settings) -> None:
    """The strictness PyJWT does not impose, and the reason the claim was added.

    PyJWT accepts `aud: ["wasla-api", "anything"]` — membership, not equality.
    This application has never minted an array, so one can only come from a
    second issuer holding the signing key, which is precisely the situation the
    audit said to get ahead of.
    """
    with pytest.raises(AuthenticationError):
        decode_token(
            _forge(aud=[ACCESS_AUDIENCE, "some-other-service"]),
            settings=settings,
            expected_type=TokenType.ACCESS,
        )


def test_an_audience_array_of_one_is_refused_too(settings: Settings) -> None:
    """The shape a library would most plausibly emit by accident."""
    with pytest.raises(AuthenticationError):
        decode_token(
            _forge(aud=[ACCESS_AUDIENCE]),
            settings=settings,
            expected_type=TokenType.ACCESS,
        )


# ------------------------------------------------------- cross-purpose use


def test_a_real_refresh_token_is_refused_where_an_access_token_belongs(
    settings: Settings,
) -> None:
    """Not a forgery — a token this application genuinely minted, at the wrong seam."""
    token, _ = create_refresh_token(settings=settings, subject=uuid.uuid4(), token_version=0)

    with pytest.raises(AuthenticationError):
        decode_token(token, settings=settings, expected_type=TokenType.ACCESS)


def test_a_real_access_token_is_refused_where_a_refresh_token_belongs(
    settings: Settings,
) -> None:
    token, _ = create_access_token(settings=settings, subject=uuid.uuid4(), token_version=0)

    with pytest.raises(AuthenticationError):
        decode_token(token, settings=settings, expected_type=TokenType.REFRESH)


def test_the_audience_refuses_a_cross_purpose_token_on_its_own(settings: Settings) -> None:
    """With `typ` corrected to lie, the audience is the only check left.

    This is what makes the two claims independent rather than one written
    twice: a forgery that gets `typ` right and `aud` wrong is still refused, so
    a regression in the `typ` comparison would not open the seam.
    """
    refresh_shaped = _forge(aud=REFRESH_AUDIENCE, typ=TokenType.ACCESS.value)

    with pytest.raises(AuthenticationError):
        decode_token(refresh_shaped, settings=settings, expected_type=TokenType.ACCESS)


# ------------------------------------------------------------ error hygiene


def test_no_refusal_says_the_audience_was_the_problem(settings: Settings) -> None:
    """A wrong audience must be indistinguishable from a wrong signature.

    Otherwise the verifier is an oracle: somebody probing learns which part of
    a forgery to fix next, one claim at a time.
    """
    forgeries = [
        _forge(aud="wasla-something-else"),
        jwt.encode(
            {key: value for key, value in _claims().items() if key != "aud"},
            SECRET,
            algorithm="HS256",
        ),
        jwt.encode(_claims(), "another-secret-entirely-long-enough", algorithm="HS256"),
        _forge(iss="https://evil.example"),
    ]

    messages = set()
    for token in forgeries:
        with pytest.raises(AuthenticationError) as caught:
            decode_token(token, settings=settings, expected_type=TokenType.ACCESS)
        messages.add(str(caught.value))

    assert messages == {"The credentials are not valid."}
