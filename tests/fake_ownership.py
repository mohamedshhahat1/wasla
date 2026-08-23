"""A stand-in for Meta's answer to "does this credential control this number?".

Shared rather than redefined per test file, so that every test which connects a
number goes through the same shape of verification the real service does. A test
that constructs `WhatsAppAccountService` without a verifier gets a 503 - which is
correct, and is why this exists.

The fake never sees a real credential and never returns one. What it records is
*that* a token was presented and what it was, so a test can assert the token
reached verification without any test needing to assert on a stored secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.integrations.whatsapp.ownership import NumberOwnershipError, VerifiedNumber


@dataclass
class FakeOwnershipVerifier:
    """Answers ownership questions from a fixed script.

    `owned` maps a phone number id to the business account that owns it.
    Anything absent is refused, which mirrors the real failure: a token that
    cannot read the node proves nothing.
    """

    owned: dict[str, str] = field(default_factory=dict)
    display_numbers: dict[str, str] = field(default_factory=dict)
    verified_names: dict[str, str] = field(default_factory=dict)
    # Set to make every call fail, for the timeout and outage paths.
    always_fails: bool = False
    calls: list[dict[str, str | None]] = field(default_factory=list)

    def owns(self, phone_number_id: str, *, waba_id: str = "555000111") -> FakeOwnershipVerifier:
        """Declare that the credential controls this number. Chainable."""
        self.owned[phone_number_id] = waba_id
        return self

    async def verify(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        claimed_waba_id: str | None = None,
    ) -> VerifiedNumber:
        self.calls.append(
            {
                "phone_number_id": phone_number_id,
                "claimed_waba_id": claimed_waba_id,
                "token": access_token,
            }
        )
        if self.always_fails:
            raise NumberOwnershipError()

        waba_id = self.owned.get(phone_number_id)
        if waba_id is None:
            raise NumberOwnershipError()
        if claimed_waba_id is not None and claimed_waba_id != waba_id:
            raise NumberOwnershipError()

        return VerifiedNumber(
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            display_phone_number=self.display_numbers.get(phone_number_id, "+201000000000"),
            verified_name=self.verified_names.get(phone_number_id),
        )
