"""Recognising a customer asking to stop.

This matcher is crude on purpose, so what these tests pin down is the shape of
the crudeness: it reads the whole message or nothing, it forgives punctuation
and keyboards, and it never guesses at a sentence.

The asymmetry is worth restating because it is what the boundary was chosen
from. A false positive stops marketing to somebody who did not quite mean it,
which a colleague undoes in a moment. A false negative keeps sending promotional
messages to somebody who has asked twice, which is how a business loses its
number.
"""

from __future__ import annotations

import pytest

from app.services.opt_out import MAX_STOP_LENGTH, is_stop_request, normalised

# Arabic written out whole rather than assembled character by character: the
# three alefs are hard to tell apart in a diff, and a word is easier to check
# against a keyboard than a list of code points.
CANCEL = "الغاء"  # cancel
CANCEL_HAMZA = "إلغاء"  # the same word as most keyboards spell it
HALT = "ايقاف"  # stop
FATHA = "َ"  # a diacritic, which a customer may or may not type


def test_a_bare_stop_is_a_stop():
    assert is_stop_request("stop") is True


def test_case_and_punctuation_do_not_matter():
    for written in ("STOP", "Stop.", "  stop  ", "!!STOP!!", "stop!"):
        assert is_stop_request(written) is True, written


def test_the_other_english_wordings_are_recognised():
    for written in ("unsubscribe", "opt out", "OptOut", "stop promotions"):
        assert is_stop_request(written) is True, written


def test_a_word_inside_a_sentence_is_not_a_stop_request():
    """The failure a substring match would make constantly."""
    for written in (
        "stop by the shop tomorrow",
        "can you stop the delivery please",
        "don't stop, keep going",
    ):
        assert is_stop_request(written) is False, written


def test_a_sentence_is_not_read_at_all():
    """Beyond a few words this stops looking and lets a person decide."""
    assert is_stop_request("please take me off your marketing list, thank you") is False


def test_nothing_is_not_a_stop_request():
    assert is_stop_request(None) is False
    assert is_stop_request("") is False
    assert is_stop_request("   ") is False


def test_a_very_long_message_is_refused_before_it_is_normalised():
    assert is_stop_request("stop" + "!" * MAX_STOP_LENGTH) is False


def test_arabic_is_recognised():
    assert is_stop_request(CANCEL) is True
    assert is_stop_request(HALT) is True


def test_the_hamza_a_keyboard_produces_does_not_hide_an_opt_out():
    """A customer's keyboard must not decide whether they are left alone."""
    assert is_stop_request(CANCEL_HAMZA) is True


def test_diacritics_are_ignored():
    assert is_stop_request(CANCEL[0] + FATHA + CANCEL[1:]) is True


def test_repeated_spaces_collapse():
    assert normalised("stop    promotions") == "stop promotions"


@pytest.mark.parametrize("written", ["hello", "شكرا", "yes please", "1"])
def test_ordinary_messages_are_left_alone(written):
    assert is_stop_request(written) is False
