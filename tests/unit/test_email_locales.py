"""Every e-mail in four languages, English untouched (LC-05, FEB-09, ADR-201)."""

from __future__ import annotations

import re
import string

import pytest

from app.core.locales import SUPPORTED_LOCALES
from app.services.email_templates import (
    _REQUIRED_KEYS,
    _SUBJECTS,
    EmailTemplate,
    render,
    subject_for,
)
from app.services.email_translations import REVIEWED, TRANSLATIONS

PUBLIC_URL = "https://app.example.test"
TRANSLATED = [code for code in SUPPORTED_LOCALES if code != "en"]


def _context(template: EmailTemplate) -> dict[str, str]:
    return {key: f"v-{key}" for key in _REQUIRED_KEYS[template]}


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_translated_language_covers_every_template() -> None:
    assert set(TRANSLATIONS) == set(TRANSLATED)
    for code in TRANSLATED:
        assert set(TRANSLATIONS[code]) == set(EmailTemplate), code


@pytest.mark.parametrize("code", TRANSLATED)
def test_the_drafts_are_marked_unreviewed(code: str) -> None:
    # FEQ-08: the owner reviews them; until then they say what they are.
    assert REVIEWED[code] is False


@pytest.mark.parametrize("code", TRANSLATED)
@pytest.mark.parametrize("template", list(EmailTemplate))
def test_a_translation_uses_exactly_the_template_s_own_values(
    code: str, template: EmailTemplate
) -> None:
    translation = TRANSLATIONS[code][template]
    used = set().union(*(_placeholders(p) for p in translation.paragraphs))
    # The token travels only inside the link, never in the prose.
    expected = _REQUIRED_KEYS[template] - {"token"}
    assert used == expected, (code, template)
    assert translation.subject.strip()
    assert _placeholders(translation.subject) == set()
    has_link = template in (EmailTemplate.WORKSPACE_INVITATION, EmailTemplate.PASSWORD_RESET)
    assert (translation.link_label is not None) is has_link


@pytest.mark.parametrize("code", TRANSLATED)
@pytest.mark.parametrize("template", list(EmailTemplate))
def test_every_template_renders_in_every_language(code: str, template: EmailTemplate) -> None:
    rendered = render(template, _context(template), public_url=PUBLIC_URL, locale=code)
    assert rendered.subject == subject_for(template, code)
    assert rendered.subject != _SUBJECTS[template]
    assert f'<html lang="{code}"' in rendered.html
    assert ('dir="rtl"' in rendered.html) is (code == "ar")
    for value in _context(template).values():
        if value != "v-token":
            assert value in rendered.text
            assert f"<bdi>{value}</bdi>" in rendered.html or value in rendered.html
    assert "{" not in rendered.text and "}" not in rendered.text


@pytest.mark.parametrize("template", list(EmailTemplate))
def test_english_is_the_default_and_unchanged(template: EmailTemplate) -> None:
    context = _context(template)
    assert render(template, context, public_url=PUBLIC_URL) == render(
        template, context, public_url=PUBLIC_URL, locale="en"
    )
    assert subject_for(template) == _SUBJECTS[template]


def test_an_unknown_language_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="unsupported email locale"):
        render(EmailTemplate.PASSWORD_CHANGED, {}, public_url=PUBLIC_URL, locale="de")
    with pytest.raises(ValueError, match="unsupported email locale"):
        subject_for(EmailTemplate.PASSWORD_CHANGED, "de")


@pytest.mark.parametrize("code", TRANSLATED)
def test_translated_links_are_built_on_the_configured_origin(code: str) -> None:
    rendered = render(
        EmailTemplate.PASSWORD_RESET,
        {"token": "https://evil.test/x"},
        public_url=PUBLIC_URL,
        locale=code,
    )
    assert f"{PUBLIC_URL}/reset-password?token=https%3A%2F%2Fevil.test%2Fx" in rendered.text
    assert 'href="https://evil' not in rendered.html


@pytest.mark.parametrize("code", TRANSLATED)
def test_a_value_is_escaped_into_translated_html(code: str) -> None:
    rendered = render(
        EmailTemplate.TRIAL_EXPIRED,
        {"workspace_name": "<script>x</script>"},
        public_url=PUBLIC_URL,
        locale=code,
    )
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html


@pytest.mark.parametrize("code", TRANSLATED)
def test_the_verification_code_keeps_latin_digits_left_to_right(code: str) -> None:
    rendered = render(
        EmailTemplate.EMAIL_VERIFICATION,
        {"code": "123456", "expires_minutes": "10"},
        public_url=PUBLIC_URL,
        locale=code,
    )
    assert re.search(r'<strong dir="ltr"[^>]*>123456</strong>', rendered.html)
    assert "    123456" in rendered.text
