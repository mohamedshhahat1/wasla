"""The six-digit code itself: how it is made, stored, compared and parsed.

These are the properties the rest of the feature assumes and never re-checks.
If `generate_verification_code` ever drops a leading zero the keyspace shrinks
by a tenth and nothing above this file would notice; if `hash_verification_code`
ever became SHA-256 the storage argument in
`app/db/models/email_verification.py` would silently stop being true.

Argon2 is slow on purpose, which is the point of using it here and a real cost
in a test file. Anything asserting a *shape* rather than the hash substitutes a
cheap stand-in; anything asserting the hash itself pays for a small number of
real hashes and no more.
"""

from __future__ import annotations

import secrets

import pytest

from app.core import security
from app.core.security import (
    VERIFICATION_CODE_DIGITS,
    generate_verification_code,
    hash_verification_code,
    normalise_verification_code,
    spend_code_verification_time,
    verify_verification_code,
)

# A value with a leading zero, chosen so a generator that built digits
# arithmetically would produce "42" and fail visibly.
CANARY = "000042"


@pytest.fixture
def cheap_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace Argon2 for tests that are about the code, not the hash.

    Deliberately not a no-op: it still returns something hash-shaped, so a test
    that accidentally asserts on the stored value fails rather than passing
    against an empty string.
    """
    monkeypatch.setattr(security, "hash_verification_code", lambda code: f"stub:{code}")


# ----------------------------------------------------------------- generation


def test_a_code_is_always_exactly_six_decimal_digits(cheap_hash: None) -> None:
    """The property, over enough samples to catch a formatting mistake.

    Not a proof - it is a random generator - but a generator that drops leading
    zeroes fails this roughly a tenth of the time per sample, so sixty samples
    make the mistake essentially certain to surface.
    """
    for _ in range(60):
        code, _ = generate_verification_code()
        assert len(code) == VERIFICATION_CODE_DIGITS
        assert code.isascii()
        assert code.isdigit()


def test_a_leading_zero_survives(monkeypatch: pytest.MonkeyPatch, cheap_hash: None) -> None:
    """`000042` is an ordinary code, not a bug to be normalised away.

    Driven rather than waited for: a millionth of the keyspace is not something
    to sample for.
    """
    monkeypatch.setattr(secrets, "randbelow", lambda upper: 42)
    code, _ = generate_verification_code()
    assert code == CANARY


def test_the_generator_draws_from_the_cryptographic_source(
    monkeypatch: pytest.MonkeyPatch,
    cheap_hash: None,
) -> None:
    """`secrets`, not `random`.

    Asserted by observing the call, because the difference is invisible in the
    output: `random.randrange` would produce codes that look identical to these
    and are predictable from a few observations. A future refactor that reached
    for the convenient module would leave every other test in this file green.
    """
    calls: list[int] = []

    def _spy(upper: int) -> int:
        calls.append(upper)
        return 7

    monkeypatch.setattr(secrets, "randbelow", _spy)
    generate_verification_code()
    assert calls == [10**VERIFICATION_CODE_DIGITS]


def test_two_codes_are_not_the_same_value(cheap_hash: None) -> None:
    """A constant generator would pass every other test in this file."""
    codes = {generate_verification_code()[0] for _ in range(20)}
    assert len(codes) > 1


# -------------------------------------------------------------------- storage


def test_the_stored_value_is_an_argon2_hash_and_not_the_code() -> None:
    code, code_hash = generate_verification_code()
    assert code not in code_hash
    assert code_hash.startswith("$argon2")


def test_the_same_code_hashes_differently_each_time() -> None:
    """Salted per row, which is why a challenge is found by account.

    An unsalted digest would make the hash a lookup key - and would make a
    leaked database a rainbow table of a million entries.
    """
    first = hash_verification_code(CANARY)
    second = hash_verification_code(CANARY)
    assert first != second
    assert verify_verification_code(code=CANARY, code_hash=first)
    assert verify_verification_code(code=CANARY, code_hash=second)


def test_a_wrong_code_does_not_verify() -> None:
    _, code_hash = generate_verification_code()
    assert not verify_verification_code(code="000000", code_hash=code_hash)


@pytest.mark.parametrize("stored", ["", "not-a-hash", "$argon2id$v=19$m=1,t=1,p=1$aaaa$bbbb"])
def test_an_unreadable_stored_value_is_a_failed_attempt_not_a_crash(stored: str) -> None:
    """A corrupt verifier must answer "no", the way a wrong password does.

    Raising here would turn a data problem into a 500 on a security endpoint,
    and a 500 that only happens for some accounts is itself an oracle.
    """
    assert not verify_verification_code(code=CANARY, code_hash=stored)


def test_spending_time_on_a_hopeless_check_neither_raises_nor_succeeds() -> None:
    """The timing equaliser used when there is no challenge to check against."""
    # Returns nothing; what it is for is taking the same time as a real
    # verification, which is a timing property rather than a value.
    spend_code_verification_time(CANARY)


# ------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "submitted",
    ["482731", " 482731 ", "482 731", "482-731", "48-27-31"],
)
def test_the_shapes_people_actually_paste_are_accepted(submitted: str) -> None:
    """Spaces and hyphens come from mail clients, not from attackers."""
    assert normalise_verification_code(submitted) == "482731"


def test_a_leading_zero_code_parses_as_itself() -> None:
    assert normalise_verification_code(CANARY) == CANARY


@pytest.mark.parametrize(
    "submitted",
    [
        "",
        "12345",
        "1234567",
        "abcdef",
        "48273a",
        "12345 ",
        "٤٨٢٧٣١",
        "482731\n482731",
        "  ",
        "+48273",
        "4.8273",
        "۴۸۲۷۳۱",
    ],
)
def test_anything_that_is_not_six_ascii_digits_is_refused(submitted: str) -> None:
    """Including numerals Python calls digits.

    `"٤٨٢٧٣١".isdigit()` is `True`, and this product's users type Arabic. A
    parser written with `.isdigit()` alone would accept those, hash them,
    compare them against a verifier built from Western digits and burn an
    attempt - which to the person holding the correct code is indistinguishable
    from the product being broken.
    """
    assert normalise_verification_code(submitted) is None


# Written as escapes rather than as literals. Every one of these is a confusable
# - that is the point of the test - and pasting them into source makes the file
# itself something a reviewer cannot read reliably.
HOSTILE = (
    "\x00" * 6,  # NULs
    "\u0660" * 6,  # Arabic-Indic zero, which `str.isdigit` accepts
    "\u06f4\u06f8\u06f2\u06f7\u06f3\u06f1",  # Extended Arabic-Indic
    "\U0001d7d2\U0001d7d6\U0001d7d0\U0001d7d5\U0001d7d1\U0001d7cf",  # Bold
    "\u200b" * 6,  # Zero-width spaces: six characters, none of them digits
    "\uff14\uff18\uff12\uff17\uff13\uff11",  # Fullwidth digits
)


@pytest.mark.parametrize("submitted", HOSTILE)
def test_parsing_never_raises_on_hostile_input(submitted: str) -> None:
    """Whatever arrives, the answer is a code or None - never an exception.

    Most of these answer `True` to `str.isdigit()`, and a parser that trusted
    it would hash them, compare them against a verifier built from ASCII digits
    and burn an attempt - which to the person holding the right code is
    indistinguishable from the product being broken.
    """
    assert normalise_verification_code(submitted) is None
