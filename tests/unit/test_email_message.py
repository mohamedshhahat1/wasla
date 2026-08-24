"""What `EmailMessage` refuses to be.

The class exists to make header injection unrepresentable rather than merely
unlikely, so these tests are mostly about construction failing. Each one names
the attack it stands in for: a newline in an address is an extra header, a NUL
is a truncated one, and a recipient list of fifty is somebody testing whether
this is a relay.
"""

from __future__ import annotations

import pytest

from app.integrations.email.base import (
    MAX_ADDRESS_LENGTH,
    MAX_BODY_BYTES,
    MAX_RECIPIENTS,
    MAX_SUBJECT_LENGTH,
    EmailMessage,
    validate_address,
)


def _message(**overrides):
    fields = {
        "sender": "no-reply@example.com",
        "to": ("person@example.com",),
        "subject": "A subject",
        "text": "A body.",
    }
    fields.update(overrides)
    return EmailMessage(**fields)


def test_a_well_formed_message_is_accepted():
    message = _message()

    assert message.to == ("person@example.com",)
    assert message.subject == "A subject"


@pytest.mark.parametrize("injection", ["\r", "\n", "\r\n", "\x00"])
def test_a_control_character_in_a_recipient_is_refused(injection):
    with pytest.raises(ValueError):
        _message(to=(f"person@example.com{injection}Bcc: attacker@evil.test",))


@pytest.mark.parametrize("injection", ["\r", "\n", "\r\n", "\x00"])
def test_a_control_character_in_the_sender_is_refused(injection):
    with pytest.raises(ValueError):
        _message(sender=f"no-reply@example.com{injection}Bcc: attacker@evil.test")


@pytest.mark.parametrize("injection", ["\r", "\n", "\r\n", "\x00"])
def test_a_control_character_in_the_subject_is_refused(injection):
    with pytest.raises(ValueError):
        _message(subject=f"Hello{injection}Bcc: attacker@evil.test")


@pytest.mark.parametrize("injection", ["\r", "\n", "\r\n", "\x00"])
def test_a_control_character_in_the_reply_to_is_refused(injection):
    with pytest.raises(ValueError):
        _message(reply_to=f"reply@example.com{injection}X-Evil: 1")


def test_a_message_with_no_recipients_is_refused():
    with pytest.raises(ValueError):
        _message(to=())


def test_more_recipients_than_the_cap_are_refused():
    recipients = tuple(f"person{index}@example.com" for index in range(MAX_RECIPIENTS + 1))

    with pytest.raises(ValueError):
        _message(to=recipients)


def test_exactly_the_recipient_cap_is_allowed():
    recipients = tuple(f"person{index}@example.com" for index in range(MAX_RECIPIENTS))

    assert len(_message(to=recipients).to) == MAX_RECIPIENTS


def test_an_oversized_subject_is_refused():
    with pytest.raises(ValueError):
        _message(subject="s" * (MAX_SUBJECT_LENGTH + 1))


def test_an_empty_subject_is_refused():
    with pytest.raises(ValueError):
        _message(subject="   ")


def test_an_empty_text_body_is_refused():
    """A message with nothing in it is a bug upstream, not a message."""
    with pytest.raises(ValueError):
        _message(text="   ")


def test_an_oversized_text_body_is_refused():
    with pytest.raises(ValueError):
        _message(text="b" * (MAX_BODY_BYTES + 1))


def test_an_oversized_html_body_is_refused():
    with pytest.raises(ValueError):
        _message(html="<p>" + "b" * (MAX_BODY_BYTES + 1) + "</p>")


def test_the_body_cap_counts_bytes_rather_than_characters():
    """A multi-byte character must not buy four times the payload."""
    with pytest.raises(ValueError):
        _message(text="一" * MAX_BODY_BYTES)


def test_an_oversized_address_is_refused():
    with pytest.raises(ValueError):
        validate_address("a" * MAX_ADDRESS_LENGTH + "@example.com")


@pytest.mark.parametrize(
    "address",
    ["", "   ", "no-at-sign", "@example.com", "person@", "person@example", "a b@example.com"],
)
def test_a_malformed_address_is_refused(address):
    with pytest.raises(ValueError):
        validate_address(address)


def test_surrounding_whitespace_is_stripped_from_an_address():
    assert validate_address("  person@example.com  ") == "person@example.com"


def test_a_message_exposes_no_way_to_set_a_header():
    """The absence is the control: what cannot be expressed cannot be abused."""
    assert not hasattr(_message(), "headers")
