"""The grounding guard's rules, and its false-positive rate on the eval corpus (RQ-19, P-R24).

The false-positive rate is measured on replies built from the labelled answer
spans of `tests/fixtures/rag/eval/questions.jsonl`: each reply quotes the price
its span holds - as written, in Arabic-Indic digits, and with the thousands
separator moved - and its source is that span. A guard that flags any of them
is flagging a correct answer.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.number_grounding import CURRENCY, PERCENT, check, quoted_numbers

QUESTIONS = Path(__file__).resolve().parents[1] / "fixtures" / "rag" / "eval" / "questions.jsonl"
ARABIC_DIGITS = {str(n): chr(0x0660 + n) for n in range(10)}


def _arabic(text: str) -> str:
    return "".join(ARABIC_DIGITS.get(ch, ch) for ch in text)


def test_amounts_and_percentages_are_found_in_both_scripts() -> None:
    found = quoted_numbers(
        f"Premium is 7,900 EGP; {_arabic('4500')} جنيه; $15; 10% off;"
        f" {_arabic('5')}{chr(0x066A)}; 3 percent"
    )
    assert {(n.value, n.kind) for n in found} == {
        (Decimal(7900), CURRENCY),
        (Decimal(4500), CURRENCY),
        (Decimal(15), CURRENCY),
        (Decimal(10), PERCENT),
        (Decimal(5), PERCENT),
        (Decimal(3), PERCENT),
    }


def test_dates_phones_and_model_numbers_are_not_checked() -> None:
    assert quoted_numbers("Model BK-35, call 01001234567 on 12/10/2026, 3 years warranty") == []


def test_a_number_in_arabic_indic_digits_is_the_same_number() -> None:
    """M-R38: ٤٥٠٠ is 4500."""
    verdict = check(
        f"السعر {_arabic('4500')} جنيه",
        retrieved=["Economy finishing costs 4500 EGP per square metre."],
        customer=[],
    )
    assert verdict.checked and not verdict.ungrounded


def test_arithmetic_on_grounded_numbers_is_grounded() -> None:
    verdict = check(
        "For 150 m at 4,500 EGP that is 675,000 EGP, plus 50 EGP delivery: 675,050 EGP.",
        retrieved=["Economy finishing costs 4500 EGP per square metre."],
        customer=["my apartment is 150 m"],
        instructions="Delivery costs 50 EGP everywhere.",
    )
    assert verdict.grounded == 4 and not verdict.ungrounded


def test_a_price_no_source_states_is_ungrounded() -> None:
    verdict = check(
        "Premium finishing is 8,200 EGP and you get 15% off.",
        retrieved=["Premium finishing costs 7900 EGP per square metre."],
        customer=["how much is premium?"],
    )
    assert {n.value for n in verdict.ungrounded} == {Decimal(8200), Decimal(15)}


def _span_replies() -> list[tuple[str, str]]:
    replies = []
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        question = json.loads(line)
        span = question.get("span") or ""
        if not quoted_numbers(span) and not re.search(r"\d", span):
            continue
        replies.append((f"According to our price list, {span}.", span))
        replies.append((f"السعر: {_arabic(span)}", span))
    return replies


def test_the_false_positive_rate_on_correct_replies_is_under_two_percent() -> None:
    replies = _span_replies()
    checked = flagged = 0
    for reply, span in replies:
        verdict = check(reply, retrieved=[span], customer=[])
        checked += len(verdict.checked)
        flagged += len(verdict.ungrounded)
    # Non-vacuity: hundreds of prices were checked.
    assert checked > 300
    assert flagged / checked < 0.02, (flagged, checked)


@pytest.mark.parametrize("shift", [100, 1, 1000])
def test_a_changed_price_in_a_correct_reply_is_caught(shift: int) -> None:
    caught = total = 0
    for reply, span in _span_replies():
        numbers = quoted_numbers(reply)
        if not numbers:
            continue
        value = numbers[0].value
        wrong = reply.replace(
            f"{int(value):,}" if f"{int(value):,}" in reply else str(int(value)),
            str(int(value) + shift),
            1,
        )
        if wrong == reply:
            continue
        total += 1
        caught += int(bool(check(wrong, retrieved=[span], customer=[]).ungrounded))
    assert total > 100
    assert caught / total > 0.95
