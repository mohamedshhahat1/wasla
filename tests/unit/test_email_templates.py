"""What a rendered email may and may not contain.

Three properties carry the security of this module, and each has tests here:
a subject is a constant so nothing caller-influenced reaches a header, every
variable is HTML-escaped into the HTML body, and every link is the configured
origin plus a literal path - so no variable is ever a URL and no emailed link
can be aimed somewhere a caller chose.
"""

from __future__ import annotations

import pytest

from app.services.email_templates import (
    EmailTemplate,
    RenderedEmail,
    render,
    subject_for,
)

PUBLIC_URL = "https://app.example.com"

# What each template needs to render. Kept beside the tests rather than
# imported from the module under test, so a required key silently disappearing
# is a failure here rather than a change both sides agree to.
CONTEXTS: dict[EmailTemplate, dict[str, str]] = {
    EmailTemplate.WORKSPACE_INVITATION: {"workspace_name": "Acme", "token": "tok-1"},
    EmailTemplate.PASSWORD_RESET: {"token": "tok-2"},
    EmailTemplate.PASSWORD_CHANGED: {},
    EmailTemplate.SESSIONS_REVOKED: {},
    EmailTemplate.ACCOUNT_DISABLED: {},
    EmailTemplate.ACCOUNT_ENABLED: {},
    EmailTemplate.INVOICE_ISSUED: {
        "amount_due": "49.00",
        "currency": "USD",
        "period_start": "2026-08-01",
        "period_end": "2026-08-31",
    },
    EmailTemplate.TRIAL_EXPIRED: {"workspace_name": "Acme"},
    EmailTemplate.SUBSCRIPTION_CANCELLED: {"workspace_name": "Acme"},
}


def _render(template: EmailTemplate, **overrides) -> RenderedEmail:
    context = dict(CONTEXTS[template])
    context.update(overrides)
    return render(template, context, public_url=PUBLIC_URL)


@pytest.mark.parametrize("template", list(EmailTemplate))
def test_every_template_renders(template):
    """A template in the enum with no rendering branch is a permanent failure."""
    rendered = _render(template)

    assert rendered.subject
    assert rendered.text.strip()
    assert rendered.html.strip()


@pytest.mark.parametrize("template", list(EmailTemplate))
def test_every_subject_is_the_constant_for_its_template(template):
    assert _render(template).subject == subject_for(template)


@pytest.mark.parametrize("template", list(EmailTemplate))
def test_no_subject_carries_a_control_character(template):
    """A subject travels in a header, so this is the header-injection floor."""
    subject = _render(template).subject

    assert "\r" not in subject
    assert "\n" not in subject
    assert "\x00" not in subject


@pytest.mark.parametrize(
    "template",
    [
        EmailTemplate.WORKSPACE_INVITATION,
        EmailTemplate.TRIAL_EXPIRED,
        EmailTemplate.SUBSCRIPTION_CANCELLED,
    ],
)
def test_a_workspace_name_is_escaped_into_the_html(template):
    """The only caller-influenced value in any template, and it is escaped."""
    rendered = _render(template, workspace_name="<script>alert(1)</script>")

    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html


def test_a_workspace_name_cannot_break_out_of_an_attribute():
    rendered = _render(
        EmailTemplate.WORKSPACE_INVITATION,
        workspace_name='" onmouseover="alert(1)',
    )

    assert 'onmouseover="alert(1)' not in rendered.html


def test_an_invoice_amount_is_escaped_into_the_html():
    rendered = _render(EmailTemplate.INVOICE_ISSUED, amount_due="<b>1</b>")

    assert "<b>1</b>" not in rendered.html
    assert "&lt;b&gt;" in rendered.html


def test_an_invitation_link_is_built_on_the_configured_origin():
    rendered = _render(EmailTemplate.WORKSPACE_INVITATION, token="abc123")

    assert f"{PUBLIC_URL}/invitations/accept?token=abc123" in rendered.text


def test_a_reset_link_is_built_on_the_configured_origin():
    rendered = _render(EmailTemplate.PASSWORD_RESET, token="abc123")

    assert f"{PUBLIC_URL}/reset-password?token=abc123" in rendered.text


def test_a_token_is_url_encoded_into_the_query():
    """A token is opaque; a token containing a `&` must not become two params."""
    rendered = _render(EmailTemplate.PASSWORD_RESET, token="a&b=c d/e")

    assert "a%26b%3Dc+d%2Fe" in rendered.text
    assert "?token=a&b=c" not in rendered.text


def test_a_token_cannot_redirect_the_link_elsewhere():
    """No variable is ever a URL, so a token full of URL cannot become one."""
    rendered = _render(EmailTemplate.PASSWORD_RESET, token="https://evil.test/steal")

    assert "https://evil.test/steal" not in rendered.text
    assert rendered.text.count(PUBLIC_URL) >= 1


def test_a_trailing_slash_on_the_origin_does_not_double():
    rendered = render(
        EmailTemplate.PASSWORD_RESET,
        {"token": "t"},
        public_url="https://app.example.com/",
    )

    assert "https://app.example.com/reset-password?token=t" in rendered.text
    assert "//reset-password" not in rendered.text


@pytest.mark.parametrize(
    ("template", "missing"),
    [
        (EmailTemplate.PASSWORD_RESET, "token"),
        (EmailTemplate.WORKSPACE_INVITATION, "token"),
        (EmailTemplate.WORKSPACE_INVITATION, "workspace_name"),
        (EmailTemplate.INVOICE_ISSUED, "currency"),
        (EmailTemplate.TRIAL_EXPIRED, "workspace_name"),
    ],
)
def test_a_missing_context_key_refuses_to_render(template, missing):
    """Better a permanent failure than an email with a hole where a link goes."""
    context = dict(CONTEXTS[template])
    del context[missing]

    with pytest.raises(ValueError, match=missing):
        render(template, context, public_url=PUBLIC_URL)


def test_a_link_template_without_an_origin_refuses_to_render():
    with pytest.raises(ValueError, match="public_url"):
        render(EmailTemplate.PASSWORD_RESET, {"token": "t"}, public_url="")


@pytest.mark.parametrize("template", list(EmailTemplate))
def test_no_template_carries_an_unsubscribe_link(template):
    """Transactional only: a security notice is not something to opt out of."""
    rendered = _render(template)

    assert "unsubscribe" not in rendered.text.lower()
    assert "unsubscribe" not in rendered.html.lower()


def test_the_invoice_template_carries_no_payment_link():
    """A bill with a link in it is the shape every invoice-phishing mail takes."""
    rendered = _render(EmailTemplate.INVOICE_ISSUED)

    assert "href=" not in rendered.html
    assert "http" not in rendered.text.replace(PUBLIC_URL, "")


@pytest.mark.parametrize(
    "template",
    [
        EmailTemplate.PASSWORD_CHANGED,
        EmailTemplate.SESSIONS_REVOKED,
        EmailTemplate.ACCOUNT_DISABLED,
        EmailTemplate.ACCOUNT_ENABLED,
    ],
)
def test_a_security_notice_takes_no_variables_at_all(template):
    """Nothing to interpolate is nothing to escape wrongly."""
    rendered = render(template, {}, public_url=PUBLIC_URL)

    assert rendered.text.strip()
    assert "href=" not in rendered.html
