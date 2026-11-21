"""An in-memory Meta for Embedded Signup's five Graph calls (FK-04, ADR-204).

The shapes are Meta's as its onboarding pages show them (read 2026-11-21); the
outcome is chosen per value - a code, a number - never by a global switch, and
every call is logged so a test can say what Meta was and was not asked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.integrations.whatsapp.onboarding import (
    EmbeddedSignupCodeExpiredError,
    EmbeddedSignupCodeInvalidError,
    ExchangedToken,
    NumberPlatform,
    OnboardingStepError,
)

#: Codes the fake answers specially, as the e2e fakes do (FK-02).
EXPIRED_CODE = "es-expired"
INVALID_CODE = "es-invalid"
#: The business token the fake issues for any other code.
BUSINESS_TOKEN = "EAAB-es-business-token-never-shown"


@dataclass
class FakeOnboardingClient:
    """Answers like Meta; logs every call (FK-03)."""

    coexistence_numbers: set[str] = field(default_factory=set)
    failing: set[str] = field(default_factory=set)
    unreadable_platform: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    _requests: int = 0

    def coexists(self, phone_number_id: str) -> FakeOnboardingClient:
        self.coexistence_numbers.add(phone_number_id)
        return self

    def fails(self, *steps: str) -> FakeOnboardingClient:
        self.failing.update(steps)
        return self

    def steps(self) -> list[str]:
        return [call["step"] for call in self.calls]

    async def exchange_code(self, code: str) -> ExchangedToken:
        self.calls.append({"step": "exchange", "code": code})
        if code == EXPIRED_CODE:
            raise EmbeddedSignupCodeExpiredError()
        if code == INVALID_CODE:
            raise EmbeddedSignupCodeInvalidError()
        return ExchangedToken(access_token=BUSINESS_TOKEN, expires_in=None)

    async def subscribe(self, *, token: str, messaging_account_id: str) -> None:
        self.calls.append({"step": "subscribe", "token": token, "waba": messaging_account_id})
        self._maybe_fail("subscribe")

    async def unsubscribe(self, *, token: str, messaging_account_id: str) -> None:
        self.calls.append({"step": "unsubscribe", "token": token, "waba": messaging_account_id})
        self._maybe_fail("unsubscribe")

    async def register(self, *, token: str, phone_number_id: str, pin: str) -> None:
        self.calls.append({"step": "register", "number": phone_number_id, "pin": pin})
        self._maybe_fail("register")

    async def read_platform(self, *, token: str, phone_number_id: str) -> NumberPlatform:
        self.calls.append({"step": "coexistence", "number": phone_number_id})
        if self.unreadable_platform:
            raise OnboardingStepError("coexistence", status=500, meta_code=None)
        on_app = phone_number_id in self.coexistence_numbers
        return NumberPlatform(is_on_biz_app=on_app, platform_type="CLOUD_API")

    async def request_sync(self, *, token: str, phone_number_id: str, sync_type: str) -> str:
        self.calls.append({"step": f"sync:{sync_type}", "number": phone_number_id})
        self._maybe_fail(f"sync:{sync_type}")
        self._requests += 1
        return f"es-request-{self._requests}"

    def _maybe_fail(self, step: str) -> None:
        if step in self.failing:
            raise OnboardingStepError(step, status=400, meta_code=100)
