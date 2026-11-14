"""The e2e helpers read the code of the mail their own request caused (FEB-32).

Registration mails a code, and the explicit send supersedes it. When
registration's mail is delivered late the mailbox (newest first) can hold it
above the send's, or hold it alone while the send's is still on the way: a
reader that takes the first code it finds verifies with a superseded one. The
helpers count the code mails before the send and wait for one more.
"""

from __future__ import annotations

from typing import Any

from tests.e2e_stack.mailbox import VERIFICATION_SUBJECT, code_after, code_mails


def _mail(code: str | None, *, subject: str = VERIFICATION_SUBJECT) -> dict[str, Any]:
    return {
        "to": ["owner@example.com"],
        "subject": subject,
        "text": "",
        "html": "",
        "links": [],
        "codes": [code] if code else [],
        "sent_at": "",
    }


def _old_helper(messages: list[dict[str, Any]]) -> str | None:
    """What smoke.py and seed.py read before FEB-32: the first code in the mailbox."""
    for message in messages:
        if message["codes"]:
            return str(message["codes"][0])
    return None


def test_a_late_registration_mail_is_not_read_as_the_sends_code() -> None:
    # The send's code (222222) is valid; registration's (111111) was superseded.
    # The helper counted registration's mail before it sent (seen=1).
    only_the_late_one = [_mail("111111")]
    both = [_mail("222222"), _mail("111111")]

    # The control: the old helper verifies with the superseded code while the
    # send's mail has not arrived yet.
    assert _old_helper(only_the_late_one) == "111111"
    # The new reader waits instead, and then takes the send's.
    assert code_after(only_the_late_one, seen=1) is None
    assert code_after(both, seen=1) == "222222"


def test_mails_without_a_code_are_not_counted() -> None:
    invitation = _mail(None, subject="You are invited")
    assert code_mails([invitation]) == 0
    assert code_after([invitation], seen=0) is None
    assert code_after([_mail("333333"), invitation], seen=0) == "333333"


def test_a_resend_reads_the_newest_of_its_own() -> None:
    mailbox = [_mail("555555"), _mail("444444"), _mail("111111")]
    assert code_mails(mailbox) == 3
    assert code_after(mailbox, seen=2) == "555555"
    assert code_after(mailbox, seen=3) is None


def test_six_digits_in_an_invitation_are_not_a_code() -> None:
    """frontend.yml 43576090907: an invitation whose token held six digits was read as the
    member's verification code, and the verify failed."""
    invitation = _mail("725948", subject="You are invited to a Wasla workspace")
    assert code_mails([invitation]) == 0
    assert code_after([invitation], seen=0) is None
    assert code_after([_mail("314159"), invitation], seen=0) == "314159"
